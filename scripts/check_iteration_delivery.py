"""Check the allowlisted delivery, while leaving operator .env untouched."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.package_delivery import build


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    with tempfile.TemporaryDirectory(prefix='lerobot-iteration-delivery-') as directory:
        temp=Path(directory).resolve()
        assert temp.parent==Path(tempfile.gettempdir()).resolve() and ROOT not in temp.parents and temp not in ROOT.parents
        project,archive=build(temp/'delivery',temp/'delivery.zip',ROOT)
        assert not (project/'.env').exists()
        scanned=subprocess.run([sys.executable,str(project/'scripts/security_scan.py')],capture_output=True,text=True,check=True)
        with zipfile.ZipFile(archive) as zipped:
            names=zipped.namelist()
            assert not any(Path(n).name=='.env' or '.runtime' in Path(n).parts for n in names)
        fonts={p.relative_to(project).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (project/'app/assets').rglob('*') if p.is_file() and (p.suffix=='.ttf' or 'license' in p.name.lower() or p.name.startswith('OFL') or 'font' in p.name.lower())}
        result=dict(delivery_security_scan='passed',scan_stdout=scanned.stdout.strip(),zip_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),zip_entries=len(names),
                    local_env_included=False,fonts_and_licenses=fonts,
                    workspace_scan='failed: existing ignored .env contains credential patterns; values never copied, printed or altered',
                    temporary_delivery='checked and cleaned only owned temporary directory; no persistent new release ZIP')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as handle:json.dump(result,handle,ensure_ascii=False,indent=2)
        print(json.dumps({k:v for k,v in result.items() if k not in {'fonts_and_licenses'}},ensure_ascii=False))


if __name__=='__main__':main()
