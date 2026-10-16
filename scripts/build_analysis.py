"""Build a redacted analysis report from the checked-in metrics artifact.

The script never reads credentials or raw dataset files.
"""
from pathlib import Path
import json
import re


def main() -> None:
    report = Path(__file__).parents[1] / "analysis" / "report.md"
    if not report.is_file():
        raise SystemExit("analysis/report.md is missing")
    text = report.read_text(encoding="utf-8")
    required = ["性质", "来源", "证据", "不能回答的问题"]
    missing = [x for x in required if x not in text]
    if missing:
        raise SystemExit(f"report is missing required sections: {missing}")
    root=report.parent.parent
    claims_path=report.with_name('claims.json')
    if claims_path.is_file():
        claims=json.loads(claims_path.read_text(encoding='utf-8'))
        records=[json.loads(s) for s in (root/claims['source_calls']).read_text(encoding='utf-8').splitlines()]
        calls={r['ordinal']:r for r in records if 'ordinal' in r}
        known=set()
        for claim in claims['claims']:
            assert claim['nature'] in {'事实','推断','假设'} and claim['scope'] and claim['origin'] and claim['review_method']
            for evidence in claim['evidence']:
                original=calls[evidence['call']]['view_url']
                if original!=evidence['view_url'] or original not in text:raise SystemExit('final evidence URL differs from actual tool response')
                known.add(original)
        delivered=re.findall(r'http://127\.0\.0\.1[^\s)]+',text)
        if any(url not in known for url in delivered):raise SystemExit('unregistered final report URL')
        metrics=json.loads((root/'docs/evidence/metrics.json').read_text(encoding='utf-8'))
        for claim in claims.get('historical_claims',[]):
            for pointer,expected in claim['pointers'].items():
                actual=metrics
                for key in pointer.strip('/').split('/'):actual=actual[key]
                if actual!=expected:raise SystemExit('historical artifact pointer differs')
    print(f"validated {report}")


if __name__ == "__main__":
    main()

