"""Initialize or rotate the local private demo secret without echoing credentials.

Depends on Python's secure terminal prompt or explicit stdin and the backend's
scrypt record writer. Run on the host with the Manager private secret file path.
"""
import argparse
import getpass
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.private_demo.auth import password_record, save_secret


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--secret-file', type=Path, required=True)
    parser.add_argument('--stdin', action='store_true')
    parser.add_argument('--rotate', action='store_true')
    args = parser.parse_args()
    if args.secret_file.exists() and not args.rotate:
        parser.error('Secret already initialized; use --rotate explicitly.')
    password = sys.stdin.readline().removesuffix('\n') if args.stdin else getpass.getpass('Private demo password: ')
    save_secret(args.secret_file, password_record(password))
    print('Private demo password configured. Existing sessions are invalidated.')


if __name__ == '__main__':
    main()
