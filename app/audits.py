"""Read-only forensic tools: explicit coverage, exact counts, bounded examples."""
from __future__ import annotations

import math
from itertools import islice
from fractions import Fraction
from .data.diagnostics import DiagnosticReader
from .data.errors import QueryLimitError, ToolArgumentError
from .viewstate import normalize_view, encode_view


def frame_url(reader, settings, field, component, start, end):
    selected=f"{field}[{component}]" if component is not None else field
    evidence=dict(kind="series_query",field=field,component=component,
                  requested_frame_range={"start":start,"end":end},returned_frame_range={"start":start,"end":end},
                  page_offset=0,page_limit=end-start)
    state=normalize_view(dict(episode=reader.episode,cursor={"frame":start},series=[selected],evidence=evidence),
                         episode_count=reader.episode+1,episode_lengths={reader.episode:reader.length},fields=reader.fields)
    return encode_view(state,settings.public_base_url,settings.max_view_url_chars)


def _finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def _number(value):
    if isinstance(value,int) and abs(value)>2**53:
        raise ToolArgumentError("component","integer outside exact binary64 range; no implicit lossy conversion")
    return float(value)


def _summary(values):
    if not values: return dict(count=0,min=None,max=None,mean=None)
    return dict(count=len(values),min=min(values),max=max(values),mean=math.fsum(values)/len(values))


def _safe(value):
    if value is None: return {"type":"null"}
    if isinstance(value,float) and not math.isfinite(value):
        return {"type":"nan" if math.isnan(value) else "positive_infinity" if value>0 else "negative_infinity"}
    return value


def _selected(value, component):
    if isinstance(value,list):
        if component is None: raise ToolArgumentError("component","vectors require an explicit raw component")
        if component>=len(value): return None
        return value[component]
    if component is not None: raise ToolArgumentError("component","scalar fields require null component")
    return value


def _integrity(reader,args,start,end,settings,examples):
    fields=args["fields"]
    results={name:dict(rows=0,null_rows=0,null_components=0,nonfinite_components=0,length_histogram={},
                       histogram_truncated=False,all_zero_vectors=0,mean_zero_nonzero_vectors=0,
                       components_checked=0,nonzero_components=0,max_abs=None,examples=[]) for name in fields}
    scalars=0; stop=start; frame_issues=[]
    for offset,rows in reader.batches(list(dict.fromkeys(["frame_index",*fields])),start,end):
        for i,row in enumerate(rows):
            cost=sum(len(row[f]) if isinstance(row[f],list) else 1 for f in fields)
            if scalars+cost>getattr(settings,"max_audit_scalars",1000000): return results,stop,"scalar_limit",frame_issues
            scalars+=cost; stop=offset+i+1
            if row["frame_index"]!=offset+i and len(frame_issues)<examples:
                frame_issues.append(dict(row_offset=offset+i,frame_index=_safe(row["frame_index"])))
            for field in fields:
                result=results[field]; value=row[field]; result["rows"]+=1
                vector=isinstance(value,list); values=value if vector else [value]
                length="null" if value is None else str(len(value)) if vector else "scalar"
                histogram=result["length_histogram"]
                if length in histogram or len(histogram)<16: histogram[length]=histogram.get(length,0)+1
                else: result["histogram_truncated"]=True
                result["null_rows"]+=int(value is None)
                nulls=sum(v is None for v in values)
                nonfinite=sum(isinstance(v,float) and not math.isfinite(v) for v in values)
                result["null_components"]+=nulls; result["nonfinite_components"]+=nonfinite
                finite=[v for v in values if _finite(v)]
                result["components_checked"]+=len(values); result["nonzero_components"]+=sum(v!=0 for v in finite)
                if finite: result["max_abs"]=max(result["max_abs"] or 0,max(abs(v) for v in finite))
                if vector and values and len(finite)==len(values):
                    result["all_zero_vectors"]+=int(all(v==0 for v in values))
                    result["mean_zero_nonzero_vectors"]+=int(math.fsum(values)==0 and any(v!=0 for v in values))
                declared=reader.adapter.info.get("features",{}).get(field,{})
                shape=declared.get("shape") if isinstance(declared,dict) else None
                bad_length=bool(vector and shape and len(value)!=shape[0])
                if (nulls or nonfinite or bad_length) and len(result["examples"])<examples:
                    result["examples"].append(dict(row_offset=offset+i,frame_index=_safe(row["frame_index"]),length=length,
                        values=[dict(component=n,value=_safe(v)) for n,v in enumerate(values) if v is None or not _finite(v)][:5],
                        view_url=frame_url(reader,settings,field,None,offset+i,offset+i+1), render_available=False,
                        render_note="Diagnostic selection; strict renderer may reject this source"))
    return results,stop,None,frame_issues


def _compare(reader,args,start,end,settings,examples):
    left,right=args["left"],args["right"]; offset=args.get("right_frame_offset",0)
    for side in (left,right):
        if side['field'] not in reader.fields:raise ToolArgumentError('field','unknown comparison field')
        feature=reader.adapter.info.get('features',{}).get(side['field'],{})
        shape=feature.get('shape') if isinstance(feature,dict) else None
        if side.get('component') is not None and isinstance(shape,list) and len(shape)==1 and isinstance(shape[0],int) and side['component']>=shape[0]:
            raise ToolArgumentError('component',f"raw component must be below declared length {shape[0]}")
    lo=min(start,max(0,start+offset)); hi=max(end,min(reader.length,end+offset))
    if hi-lo>getattr(settings,"max_audit_rows",8192)*2:
        raise QueryLimitError("offset scan exceeds bounded physical-row window")
    columns=list(dict.fromkeys(["frame_index",left["field"],right["field"]]))
    records={}; duplicate_frames=set(); scalar_count=0; frame_anomalies=0
    lengths={'left':set(),'right':set()}
    for position,batch in reader.batches(columns,lo,hi):
        for i,row in enumerate(batch):
            for label,side in (('left',left),('right',right)):
                if isinstance(row[side['field']],list):lengths[label].add(len(row[side['field']]))
            scalar_count+=sum(len(row[c]) if isinstance(row[c],list) else 1 for c in columns)
            if scalar_count>getattr(settings,"max_audit_scalars",1000000):
                raise QueryLimitError("comparison scalar budget exceeded; narrow range")
            key=row["frame_index"]
            frame_anomalies+=int(key!=position+i)
            if not isinstance(key,int): continue
            if key in records: duplicate_frames.add(key)
            else: records[key]={"left":_selected(row[left["field"]],left.get("component")),"right":_selected(row[right["field"]],right.get("component"))}
    for label,side in (('left',left),('right',right)):
        if side.get('component') is not None and lengths[label] and side['component']>=max(lengths[label]):
            raise ToolArgumentError('component',f"raw component is outside observed lengths in {label} scan")
    result=dict(expected_pairs=end-start,found_pairs=0,finite_pairs=0,exact_match_count=0,mismatch_count=0,
                missing_pairs=0,duplicate_pairs=0,nonfinite_pairs=0,examples=[],right_frame_offset=offset,
                equality="finite_binary64_numeric",precision="binary64 metrics; exact numeric match counts; +0 == -0")
    result.update(physical_scan_range={"start":lo,"end":hi},frame_index_anomaly_count=frame_anomalies,
                  missing_definition="not uniquely present in bounded physical scan; no claim of global absence outside this scan")
    abs_errors=[]; square_errors=[]
    for frame in range(start,end):
        target=frame+offset
        if frame in duplicate_frames or target in duplicate_frames:
            result["duplicate_pairs"]+=1; continue
        if frame not in records or target not in records:
            result["missing_pairs"]+=1; continue
        result["found_pairs"]+=1; a,b=records[frame]["left"],records[target]["right"]
        if not _finite(a) or not _finite(b): result["nonfinite_pairs"]+=1; continue
        a,b=_number(a),_number(b); result["finite_pairs"]+=1
        equal=a==b; result["exact_match_count"]+=int(equal); result["mismatch_count"]+=int(not equal)
        error=abs(a-b)
        if not math.isfinite(error) or not math.isfinite(error*error): raise QueryLimitError("error accumulator overflow")
        abs_errors.append(error); square_errors.append(error*error)
        if not equal and len(result["examples"])<examples:
            result["examples"].append(dict(left_frame=frame,right_frame=target,left=a,right=b,
                left_view_url=frame_url(reader,settings,left["field"],left.get("component"),frame,frame+1),
                right_view_url=frame_url(reader,settings,right["field"],right.get("component"),target,target+1)))
    n=result["finite_pairs"]
    result.update(abs_error_sum=math.fsum(abs_errors),squared_error_sum=math.fsum(square_errors),
                  mae=math.fsum(abs_errors)/n if n else None,rmse=math.sqrt(math.fsum(square_errors)/n) if n else None,
                  max_abs_error=max(abs_errors) if n else None,duplicate_frame_ids=sorted(duplicate_frames)[:examples])
    result.update(pairing_complete=result["found_pairs"]==end-start,finite_coverage_complete=n==end-start)
    return result


def _timing(reader,args,start,end,settings,examples):
    ts=[]; frames=[]
    for _,batch in reader.batches(["timestamp_ns","frame_index"],start,end):
        ts.extend(r["timestamp_ns"] for r in batch); frames.extend(r["frame_index"] for r in batch)
    valid=lambda v:isinstance(v,int) and not isinstance(v,bool)
    intervals=[b-a for a,b in zip(ts,ts[1:]) if valid(a) and valid(b)]
    result=dict(timestamp_count=len(ts),invalid_timestamp_count=sum(not valid(v) for v in ts),
                duplicate_intervals=sum(x==0 for x in intervals),backward_intervals=sum(x<0 for x in intervals),
                intervals_ns=_summary(intervals),pairing="physical row offset -> sequential video frame; no timestamp search",
                frame_index_anomaly_count=sum(f!=start+i for i,f in enumerate(frames)),conversion_version="rational_ns_v1",
                conversion="round(Fraction(pts * time_base_num * 1000000000, time_base_den)); ties to even",
                pts_checked=0,pts_missing=end-start,pts_status="unavailable",pts_examples=[],
                limits="Encoding time only: no capture precision, pixel/content alignment, contact, or drift inference")
    try:
        import av
        # No strict decoder/index construction: diagnostic reads can inspect malformed PTS.
        prefix="videos/base_0_camera/rgb/image/"; row=reader.row
        template=reader.adapter.info.get("video_path")
        key=template.format(video_key="base_0_camera/rgb/image",chunk_index=row[prefix+"chunk_index"],file_index=row[prefix+"file_index"])
        path=reader.adapter.materialize(key)
        differences=[]; last_pts=None; pts_duplicates=pts_backwards=0; scanned_end=0
        with av.open(path) as container:
            stream=container.streams.video[0]; base=Fraction(stream.time_base)
            if not stream.width or not stream.height or stream.width*stream.height*32>16*1024**2:
                raise QueryLimitError("video diagnostic decoder exceeds reserved work memory")
            stream.thread_type="SLICE";stream.thread_count=1
            result["time_base"]={"numerator":base.numerator,"denominator":base.denominator}
            scan_cap=getattr(settings,"max_audit_rows",8192)
            for frame,video_frame in enumerate(islice(container.decode(stream),min(end,scan_cap))):
                scanned_end=frame+1
                pts=video_frame.pts
                if frame>start and pts is not None and last_pts is not None:
                    pts_duplicates+=int(pts==last_pts); pts_backwards+=int(pts<last_pts)
                last_pts=pts
                if frame<start or pts is None: continue
                value=ts[frame-start]
                if not valid(value): continue
                pts_ns=round(Fraction(pts)*base*1000000000)
                differences.append(value-pts_ns)
                if len(result["pts_examples"])<examples:
                    result["pts_examples"].append(dict(frame=frame,pts=pts,pts_ns=pts_ns,timestamp_ns=value,delta_ns=value-pts_ns,
                        view_url=frame_url(reader,settings,"timestamp_ns",None,frame,frame+1)))
        result.update(pts_checked=len(differences),pts_missing=len(ts)-len(differences),pts_status="complete" if len(differences)==len(ts) else "partial",
                      pts_duplicate_count=pts_duplicates,pts_backwards_count=pts_backwards,delta_ns=_summary(differences),
                      pts_scan_scope={"start_frame":0,"end_frame":scanned_end},
                      pts_unscanned_frame_ranges=([{"start":max(start,scanned_end),"end":end}] if scanned_end<end else []),
                      data_domain_semantics="[first timestamp, last timestamp + median adjacent difference)",
                      video_domain_metadata={k:row.get(prefix+k) for k in ("from_timestamp","to_timestamp")})
    except Exception as exc:
        result["pts_error"]=dict(code=type(exc).__name__,message="PTS unavailable; timestamp checks retained; no zero-anomaly claim")
    return result


def run_audit(name,args,adapter,settings):
    reader=DiagnosticReader(adapter,args["episode"])
    try:
        start=args.get("start_frame",0); end=args.get("end_frame",reader.length); cursor=args.get("cursor",0)
        if not 0<=start<end<=reader.length: raise ToolArgumentError("range","require 0 <= start < end <= physical rows")
        if not 0<=cursor<end-start: raise ToolArgumentError("cursor","offset within original range")
        page_start=start+cursor; cap=getattr(settings,"max_audit_rows",8192)
        page_end=min(end,page_start+cap); examples=args.get("max_examples",5)
        field=args["left"]["field"] if name=="compare_series" else args["fields"][0] if name=="audit_integrity" else "timestamp_ns"
        component=args["left"].get("component") if name=="compare_series" else None
        if field not in reader.fields: raise ToolArgumentError("field","unknown diagnostic field")
        try:
            if name=="compare_series": details=_compare(reader,args,page_start,page_end,settings,examples); stop=page_end; reason=None
            elif name=="audit_timing": details=_timing(reader,args,page_start,page_end,settings,examples); stop=page_end; reason=None
            else:
                details,stop,reason,frame_issues=_integrity(reader,args,page_start,page_end,settings,examples)
                details={"fields":details,"frame_index_examples":frame_issues}
            if stop==page_start: raise QueryLimitError("one diagnostic row exceeds scalar budget")
        except QueryLimitError as exc:
            return dict(episode=reader.episode,requested_frame_range={"start":start,"end":end},completed_frame_range=None,
                        uncompleted_frame_ranges=[{"start":page_start,"end":end}],complete=False,input_count=0,
                        details={"status":"failed","code":type(exc).__name__,"note":"No anomaly counters available; narrow selection and retry"},
                        truncated=True,next_cursor=None,stop_reason="work_limit_requires_narrower_request",replay_parameters=args,
                        view_url=frame_url(reader,settings,field,component,page_start,page_end),view_scope="requested selection ONLY; no rows checked")
        return dict(episode=reader.episode,requested_frame_range={"start":start,"end":end},
                    completed_frame_range={"start":page_start,"end":stop},uncompleted_frame_ranges=([{ "start":stop,"end":end}] if stop<end else []),
                    complete=stop==end and cursor==0, input_count=stop-page_start,details=details,
                    truncated=stop<end,next_cursor=stop-start if stop<end else None,
                    stop_reason=reason or ("row_limit" if stop<end else "completed"),replay_parameters=args,
                    snapshot=reader.snapshot,view_url=frame_url(reader,settings,field,component,page_start,stop),
                    view_availability="selection URL; strict renderer requires a valid dataset; diagnostic anomalies may prevent rendering")
    finally: reader.close()
