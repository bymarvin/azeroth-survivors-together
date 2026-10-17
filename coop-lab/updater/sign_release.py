"""Ed25519 release signing for the co-op self-updater (boot-log format).

  sign_release.py init               create the signing key (once)
  sign_release.py pubkey             print the public key (for self_update.TRUSTED_KEYS)
  sign_release.py sign latest.json   write latest.json.sig next to it
  sign_release.py rotate             create a new key; announce it with keys.add in the next
                                     release (signed with the current key), then switch

The private key lives only in the key folder (default C:/Users/<you>/.azcoop-signing),
readable only by you. It is never printed, logged, copied into a release or the repo.
"""
import argparse
import base64
import getpass
import os
import subprocess
import sys
import time
from pathlib import Path

KEY_DIR = Path.home() / '.azcoop-signing'
CURRENT = 'release-key.pem'


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def restrict(path):
    """Only the current Windows user may read the key (no inherited access)."""
    user = os.environ.get('USERNAME') or getpass.getuser()
    subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'{user}:F'], check=True,
                   capture_output=True)


def load(key_dir, name=CURRENT):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    path = key_dir / name
    if not path.is_file():
        raise SystemExit(f'[FAILED] No signing key at {path}; run sign_release.py init first')
    return load_pem_private_key(path.read_bytes(), password=None)


def public(key):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode('ascii')


def create(key_dir, name):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    key_dir.mkdir(parents=True, exist_ok=True)
    path = key_dir / name
    if path.exists():
        raise SystemExit(f'[FAILED] {path} already exists; refusing to overwrite a signing key')
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    restrict(path)
    return key


def sign_bytes(data, key_dir=KEY_DIR):
    return load(key_dir).sign(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('command', choices=('init', 'pubkey', 'sign', 'rotate'))
    ap.add_argument('file', nargs='?', type=Path)
    ap.add_argument('--key-dir', type=Path, default=KEY_DIR)
    args = ap.parse_args()
    if args.command == 'init':
        key = create(args.key_dir, CURRENT)
        log('  OK  ', f'Signing key created in {args.key_dir} (only your user can read it)')
        log(' INFO ', f'Public key for self_update.TRUSTED_KEYS: {public(key)}')
    elif args.command == 'pubkey':
        log(' INFO ', public(load(args.key_dir)))
    elif args.command == 'sign':
        if args.file is None or not args.file.is_file():
            raise SystemExit('[FAILED] sign needs the latest.json to sign')
        data = args.file.read_bytes()
        signature = sign_bytes(data, args.key_dir)
        args.file.with_name(args.file.name + '.sig').write_bytes(signature)
        log('  OK  ', f'Signed {args.file.name} ({len(data)} bytes) with {public(load(args.key_dir))[:12]}...')
    elif args.command == 'rotate':
        name = f'release-key-{time.strftime("%Y%m%d-%H%M%S")}.pem'
        key = create(args.key_dir, name)
        log('  OK  ', f'New signing key {name} created')
        log(' INFO ', f'1. Put it into the next release with --announce-key {public(key)} (signed with the current key)')
        log(' INFO ', f'2. After that release: rename {name} to {CURRENT} (keep the old file) and add the new key to '
                      'self_update.TRUSTED_KEYS; later releases can retire the old key with --retire-key')
    return 0


if __name__ == '__main__':
    sys.exit(main())
