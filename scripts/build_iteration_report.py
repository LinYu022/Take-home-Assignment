"""Build claim evidence from actual deterministic call records, not invented tokens."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SOURCE='docs/evidence/agent-iteration-20261016/http-checks'


def main():
    rows=[json.loads(x) for x in (ROOT/SOURCE/'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    calls={r['ordinal']:r for r in rows if 'ordinal'in r}
    summary=json.loads((ROOT/SOURCE/'summary.json').read_text(encoding='utf-8'))
    def value(n):return calls[n]['response']['result']['structuredContent']
    manifest=value(1)['manifest'];timing=value(2)['details'];comparison=value(3)['details'];integrity=value(4)['details']['fields']
    claims=[]
    def claim(id,text,scope,call_ids,kind='事实',method='按来源调用参数重放并核对 structuredContent；URL 必须与记录原文一致'):
        evidence=[dict(call=n,view_url=calls[n]['view_url']) for n in call_ids]
        claims.append(dict(id=id,statement=text,nature=kind,origin='确定性 HTTP 客户端与明确列出的客户端计算；不是 Agent 或人工核对',scope=scope,
                           evidence=evidence,review_method=method))
    claim('C01',f"数据目录报告 {manifest['total_episodes']} 个 episode、{manifest['total_frames']} 行、{len(manifest['fields'])} 个字段、{manifest['total_videos']} 个视频；任务原文：{manifest['episodes'][0]['tasks']}。",'当前唯一快照的目录与元数据',[1])
    claim('C02',f"检查 {timing['timestamp_count']} 个 timestamp_ns、{timing['intervals_ns']['count']} 个区间及 {timing['pts_checked']} 个 PTS 配对。重复区间 {timing['duplicate_intervals']}，倒退区间 {timing['backward_intervals']}。timestamp_ns−换算PTS 的统计为 {timing.get('delta_ns')}。",'[0,3029)，rational_ns_v1、精确有理数、最近整数且 ties-to-even；只证明编码时间关系',[2])
    claim('C03',f"左手 action raw0[t] 与 observed tracks raw1[t+1] 的有限配对 {comparison['finite_pairs']} 个，精确匹配 {comparison['exact_match_count']} 个，缺失 {comparison['missing_pairs']}，重复 {comparison['duplicate_pairs']}，非有限 {comparison['nonfinite_pairs']}，MAE={comparison['mae']}。",'左 t=[0,3028)，right_frame=t+1；仅一个空间分量，排除无下一帧目标的末行',[3])
    claim('C04','独立客户端逐值比较三个窗口，各3个帧对、全部63个空间分量；共9个帧对、567个标量对，全部相等。','左 [0,3)、[1513,1516)、[3025,3028) 对 observed 下一帧；不是567个独立试次，也不是全段全分量',[6,7,8,9,10,11],method='scripts/verify_tool_iteration.py 的保存后比较；左完整值等于右完整值去掉时间戳前缀')
    left=integrity['action.left_hand_tracks']
    claim('C05',f"左手 action 检查 {left['rows']} 行，精确全零向量 {left['all_zero_vectors']} 个、null 行 {left['null_rows']}、非有限分量 {left['nonfinite_components']}。末行前 raw0 最大值 {value(5)['max']}。",'全零计数为全段；raw0 最大值范围 [0,3028)，不可推广到右手',[4,5],method='audit_integrity 全分量精确零计数，aggregate_series 核验末行前分量0。若其最大值<0且唯一全零向量存在，则位置只能在末行；位置为逻辑推导，不推断物理静止')
    claim('C06','MANO 元数据声明 camera 坐标系、61原始分量，分段3+45+10+3，无时间戳前缀；完整性检查的长度分布和异常计数见来源。','声明与存储/观测分开；没有统一空间单位，不推断 betas 的生成原因',[1,4])
    claim('C07','frame3025 信息栏对照仅改变 display.info_bar；两张实际 PNG 和 manifest 已保存，浏览器/无头/MCP 同状态字节一致另由浏览器回归确认。','单帧 [3025,3026)，action.left_hand_tracks，左右 observed overlays，1024×768',[15,16],method='完整 canonical 状态深拷贝只改 info_bar；重开最终URL；核对实际解码Blob、/render、MCP的PNG字节。非有限/后方/图像外/绘制互斥计数不证明可见性')
    ep=manifest['episodes'][0]
    claim('C08',f"success={ep['success']} 的来源是 {ep['success_source']}，状态 {ep['success_status']}。",'单个 episode 的 metadata 声明，不是物理真值或总体成功率',[1])
    artifact=dict(schema_version=1,source_calls=SOURCE+'/calls.jsonl',claims=claims,
                  historical_missing=['B04旧完整逐页账本与运行代码','B05旧完整原始对话与UTF-8账本','旧三帧Agent目检的可定位完整会话'],
                  historical_metrics='docs/evidence/metrics.json; unchanged historical deterministic results, not this run',
                  agent_experiment='agent-sessions/runs/20261016-iteration-01/README.md; separate from these deterministic conclusions')
    legacy=json.loads((ROOT/'docs/evidence/metrics.json').read_text(encoding='utf-8'))
    historical=[dict(id='H01',nature='事实（历史产物所报告）',origin='历史确定性 metrics.json；非本轮Agent或人工核对',
                     statement='相机/左右手四元数单位范数最大绝对误差',
                     pointers={'/camera/quaternion_norm_max_abs_error':legacy['camera']['quaternion_norm_max_abs_error'],
                               **{f'/hands/{s}/quaternion_norm_max_abs_error':legacy['hands'][s]['quaternion_norm_max_abs_error'] for s in ('left','right')}}),
                dict(id='H02',nature='事实（历史产物所报告）',origin='历史确定性 metrics.json；非本轮MANO分量全段复核',statement='两侧MANO betas原始[48,58)全零；原因仍未知',
                     pointers={f'/hands/{s}/mano_betas_all_zero':legacy['hands'][s]['mano_betas_all_zero'] for s in ('left','right')})]
    artifact['historical_claims']=historical
    (ROOT/'analysis/claims.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2),encoding='utf-8')
    text=['# LeRobot 数据分析报告','',
          '本报告的当前结论来自真实快照的独立确定性 HTTP 复核，不冒充 Agent 或人工发现。调用、返回、图片和统计见 [原始调用账本](../'+SOURCE+'/calls.jsonl) 与 [复核摘要](../'+SOURCE+'/summary.json)。旧报告与索引保留在 history/；历史 metrics.json 和 PNG 不改写。',
          '', '## 结论、性质、来源与证据','']
    for c in claims:
        links='；'.join(f"[调用 {e['call']}]({e['view_url']})" for e in c['evidence'])
        text += [f"### {c['id']}", '',c['statement'],'',f"性质：{c['nature']}。来源：{c['origin']}。范围：{c['scope']}。",'',f'证据：{links}。','',f"复核方法：{c['review_method']}。",'']
    text += ['## 历史材料与当前缺口','',
             '旧 metrics.json 的四元数范数、全段63维关系和两侧 MANO betas=0 是历史确定性脚本结果。本轮没有为这些旧结论追加伪造的 Agent 证据或把新单分量检查扩大解释。完整三帧人工核对未执行，历史 Agent 目检缺少可定位完整记录。旧 B04 的逐页账本、代码及 B05 的UTF-8合规仍不可恢复，保留为缺失。',
             '', *[f"- {h['id']}：{h['statement']}。性质：{h['nature']}；来源：{h['origin']}；范围：该历史产物声明的全段。证据：docs/evidence/metrics.json 的 JSON pointers `{h['pointers']}`；复核：scripts/analyze_sample.py，原始数据另供，不把旧数字改记为本轮运行。" for h in historical],
             '', '本轮独立 Agent 的实际表现、客户端交付、预算和失败单独见 agent-sessions/runs/20261016-iteration-01；当前报告不据成功HTTP脚本替代该验收。',
             '', '## 训练建议与能力边界','',
             '- 核验 A[t]=O[t+1] 是标签关系，不允许把 O[t+1] 当预测 A[t] 的未来输入；通常使用截至 t 的允许观测与标签 A[t]。',
             '- 对无下一帧目标的末行可以屏蔽回归损失、从目标统计中分离，同时保留末行观测。全零不证明静止、终止、奖励或成功。',
             '- 单 episode 不能估计总体成功率。均衡诊断样本不代表总体比例；更多独立采集试次需要保留纳入/排除规则和实际结果。',
             '- 标定图像边界不是合成PNG边界，也不是有效场景ROI。overlay来自同帧observed tracks；选择action曲线不改变overlay来源。',
             '', '## 当前数据不能回答的问题','',
             '没有接触/力/触觉/深度、可见性真值、子任务阶段标签或解剖名称。暗带、快运动、小PTS差和零图像外计数，只能形成待验证假设，不能证明接触力、内容级同步、时钟漂移、丢帧或错误标注。验证接触需要明确事件定义与独立传感器、采样率、标定、滤波和时钟同步；验证像素对齐需要2D对应真值/ROI；MANO betas的原因需要生成流程说明。',
             '', '## URL 复核前提','',
             '上述原始链接按同一快照、127.0.0.1:8000生成。评审者从仓库根目录配置同一DATA_ROOT并运行 python -m app。换端口时将原URL提交 /api/view/normalize，保存原URL及返回URL的映射；不能手工拼接或修改token。结构化索引是 claims.json。','']
    (ROOT/'analysis/report.md').write_text('\n'.join(text),encoding='utf-8')
    (ROOT/'analysis/evidence-index.json').write_text(json.dumps(dict(source='claims.json',claims={c['id']:c['evidence'] for c in claims}),ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'generated {len(claims)} claims from {len(calls)} actual HTTP calls')


if __name__=='__main__':main()
