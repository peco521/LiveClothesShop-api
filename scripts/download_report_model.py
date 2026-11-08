"""Optional local CPU report interpreter: official Qwen, Apache-2.0, no API key."""
import hashlib
from pathlib import Path
from urllib.request import urlopen

REVISION = '91cad51170dc346986eccefdc2dd33a9da36ead9'
FILE = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'
SHA256 = '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
SIZE = 1117320736
ROOT = Path(__file__).resolve().parents[1] / '.local/models'

def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    target = ROOT / FILE
    if target.exists() and hashlib.file_digest(target.open('rb'), 'sha256').hexdigest() == SHA256:
        print('Official report model already verified.')
        return
    temporary = target.with_suffix('.partial')
    url = f'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/{REVISION}/{FILE}'
    digest, count = hashlib.sha256(), 0
    with urlopen(url, timeout=60) as response, temporary.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > SIZE:
                raise RuntimeError('Unexpected model size')
            digest.update(chunk)
            output.write(chunk)
    if count != SIZE or digest.hexdigest() != SHA256:
        raise RuntimeError('Model integrity check failed; partial file not activated')
    temporary.replace(target)
    with urlopen(f'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/raw/{REVISION}/LICENSE', timeout=30) as response:
        (ROOT / 'QWEN-LICENSE.txt').write_bytes(response.read())
    print(f'Official Qwen model verified: {count} bytes; Apache-2.0; SHA256 {SHA256}')

if __name__ == '__main__':
    main()
