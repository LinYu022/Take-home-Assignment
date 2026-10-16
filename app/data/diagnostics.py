"""Bounded column/row diagnostics, deliberately independent of strict table loading."""
from __future__ import annotations

import io
import pyarrow.parquet as pq
from .errors import DataNotFound, QueryLimitError

WORK_BYTES = 32 * 1024**2


class RangeFile(io.RawIOBase):
    def __init__(self, store, key, size):
        self.store, self.key, self.size, self.position = store,key,size,0
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        self.position = offset if whence==0 else self.position+offset if whence==1 else self.size+offset
        if not 0<=self.position<=self.size: raise ValueError("invalid seek")
        return self.position
    def read(self, size=-1):
        size = self.size-self.position if size<0 else min(size,self.size-self.position)
        if size>WORK_BYTES//2: raise QueryLimitError("diagnostic range read exceeds work memory")
        if not size: return b""
        value=self.store.get_range(self.key,self.position,self.position+size-1)
        self.position+=len(value); return value
    def readinto(self, buffer):
        value=self.read(len(buffer)); buffer[:len(value)]=value; return len(value)


class DiagnosticReader:
    def __init__(self, adapter, episode):
        self.adapter,self.episode=adapter,episode
        key="meta/episodes.parquet"; head=adapter.store.head(key)
        if head["size_bytes"]>1024*1024: raise QueryLimitError("diagnostic episode metadata limit")
        rows=pq.read_table(io.BytesIO(adapter.store.get_range(key,0,head["size_bytes"]-1))).to_pylist()
        selected=[r for r in rows if r.get("episode_index")==episode]
        if len(selected)!=1: raise DataNotFound("episode metadata missing or ambiguous")
        self.row=selected[0]; self.key=adapter._data_key(self.row)
        head=adapter.store.head(self.key)
        self.file=RangeFile(adapter.store,self.key,int(head["size_bytes"]))
        self.parquet=pq.ParquetFile(self.file, pre_buffer=False)
        self.length=self.parquet.metadata.num_rows
        self.fields=self.parquet.schema_arrow.names
        self.snapshot=dict(source_id=adapter.store.source_id, object_key=self.key,
                           version={k:v for k,v in head.items() if k in {"sha256","etag","version_id","size_bytes"}})

    def batches(self, columns, start, end):
        if set(columns)-set(self.fields): raise DataNotFound("unknown diagnostic field")
        offset=0
        for group in range(self.parquet.num_row_groups):
            meta=self.parquet.metadata.row_group(group)
            stop=offset+meta.num_rows
            if stop<=start: offset=stop; continue
            if offset>=end: break
            # Reserve for decompression, Arrow buffers, and bounded Python rows.
            size=sum(meta.column(i).total_uncompressed_size for i in range(meta.num_columns)
                     if meta.column(i).path_in_schema.split(".list.")[0] in columns)
            if size>WORK_BYTES//2: raise QueryLimitError("selected row group exceeds diagnostic work budget")
            position=offset
            for batch in self.parquet.iter_batches(row_groups=[group],columns=columns,batch_size=128):
                a=max(0,start-position); b=min(batch.num_rows,end-position)
                if a<b:
                    selected=batch.slice(a,b-a)
                    if selected.nbytes>WORK_BYTES//8: raise QueryLimitError("diagnostic batch memory limit")
                    yield position+a,selected.to_pylist()
                position+=batch.num_rows
                if position>=end: break
            offset=stop

    def close(self):
        self.parquet.close(); self.file.close()
