DESCRIPTION = 'Exact fixed-slot storage dispatch with losses and unmet requests'
import argparse
import json
import sys
from pathlib import Path

from .common import read, save
from .model import analyze
from .reserve import ReservationBook, BookError
from .reserve.validate import RequestError, Conflict, parse_json


def run(source):
    data, digest = read(source)
    return {'schema_version': 1, 'source_sha256': digest,
            'result': analyze(data)}


def _read_request(path):
    if str(path) == '-':
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def _run_reservation(args):
    strategy = args.strategy
    book = ReservationBook(args.db, strategy=strategy, timeout=args.timeout)
    if args.action == 'init':
        raw = _read_request(args.request)
        _, receipt = book.initialize(raw)
        print(json.dumps(receipt, sort_keys=True, ensure_ascii=False))
        return 0
    if args.action == 'status':
        print(json.dumps(book.status(), sort_keys=True, ensure_ascii=False))
        return 0
    raw = _read_request(args.request)
    if args.action == 'add':
        _, receipt = book.add_contract(raw)
    elif args.action == 'cancel':
        _, receipt = book.cancel_contract(raw)
    else:  # advance
        _, receipt = book.advance(raw)
    print(json.dumps(receipt, sort_keys=True, ensure_ascii=False))
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command')

    # v1 replay interface (unchanged).
    replay = sub.add_parser('replay', help='v1 fixed-slot request replay')
    replay.add_argument('--source', type=Path, required=True)
    replay.add_argument('--output', type=Path, required=True)

    res = sub.add_parser('reservations',
                         help='executable reservation book operations')
    res.add_argument('--db', type=Path, required=True)
    res.add_argument('--strategy', choices=('external', 'transaction'),
                     default='external')
    res.add_argument('--timeout', type=float, default=30.0)
    res.add_argument('action', choices=('init', 'add', 'cancel', 'advance',
                                        'status'))
    res.add_argument('request', nargs='?', default='-',
                     help='request JSON file (- for stdin); ignored by status')
    return parser


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    # Backwards-compatible v1 invocation: the flat --source/--output form
    # without a subcommand.
    if not argv or argv[0].startswith('--'):
        return _run_legacy(argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == 'replay':
        return _run_legacy(['--source', str(args.source),
                            '--output', str(args.output)])

    try:
        return _run_reservation(args)
    except (RequestError, Conflict, BookError, ValueError, TypeError,
            KeyError, OSError, OverflowError) as error:
        print(str(error), file=sys.stderr)
        return 2


def _run_legacy(argv):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
