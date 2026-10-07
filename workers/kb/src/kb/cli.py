"""`kb` command line: ingest | search | stats."""

import argparse
import json
import logging
import sys
from pathlib import Path

from kb.sources import DOMAINS, SOURCE_TYPES, TENANTS, TenantGuardError

EXIT_ERRORS = 1  # finished, but some files failed to parse
EXIT_USAGE = 2
EXIT_GUARD = 3  # tenant isolation guard refused the run

log = logging.getLogger("kb")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kb", description="AIOS knowledge base: ingest and hybrid search")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")
    parser.add_argument("--env-file", type=Path, help="dotenv file (default: repo-root .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="ingest a file or directory (Obsidian vault aware)")
    p.add_argument("path", type=Path)
    p.add_argument("--tenant", required=True, choices=TENANTS)
    p.add_argument("--domain", choices=DOMAINS)
    p.add_argument("--label", help="root label for file:// URIs (default: the root directory name)")
    p.add_argument("--source-type", choices=SOURCE_TYPES, help="override detected source type (e.g. audio)")
    p.add_argument("--prune", action="store_true", help="mark documents under this root that no longer exist as deleted")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; no DB or embedding calls")
    p.add_argument("--json", action="store_true", help="machine-readable report")

    s = sub.add_parser("search", help="hybrid (vector + full text) search within a tenant")
    s.add_argument("query")
    s.add_argument("--tenant", required=True, choices=TENANTS)
    s.add_argument("--domain", choices=DOMAINS)
    s.add_argument("--include-shared", action="store_true", help="also search tenant 'shared'")
    s.add_argument("-k", "--top-k", type=int, default=8)
    s.add_argument("--text-only", action="store_true", help="skip the query embedding (full text only)")
    s.add_argument("--json", action="store_true")

    st = sub.add_parser("stats", help="documents/chunks per tenant/domain/source type")
    st.add_argument("--json", action="store_true")

    sv = sub.add_parser("serve", help="run the knowledge HTTP API + MCP server")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)

    sub.add_parser("maintain", help="expire temporary/superseded memories and drop orphan chunks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.command == "ingest" and not args.verbose:
        logging.getLogger("kb").setLevel(logging.INFO)  # per-file progress is the point of ingest
    try:
        commands = {"ingest": cmd_ingest, "search": cmd_search, "stats": cmd_stats, "serve": cmd_serve,
                    "maintain": cmd_maintain}
        return commands[args.command](args)
    except TenantGuardError as exc:
        print(f"kb: REFUSED: {exc}", file=sys.stderr)
        return EXIT_GUARD
    except FileNotFoundError as exc:
        print(f"kb: not found: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # connection/embedding failures: one line, never the connection string
        if type(exc).__module__.startswith(("psycopg", "kb.embed")):
            print(f"kb: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_ERRORS
        raise


def _settings(args):
    from kb.config import load_settings

    return load_settings(args.env_file)


def cmd_ingest(args) -> int:
    from kb.ingest import IngestOptions, ingest, prepare

    root = args.path.expanduser()
    if args.prune and root.is_file():
        print("kb: --prune needs a directory", file=sys.stderr)
        return EXIT_USAGE
    opts = IngestOptions(
        tenant=args.tenant, domain=args.domain, label=args.label, source_type=args.source_type,
        prune=args.prune, dry_run=args.dry_run,
    )
    if args.dry_run:
        report = ingest(root, opts)
    else:
        prepare(root, opts)  # guard before opening any connection
        from kb.embed import Embedder
        from kb.store import PgStore, connect

        settings = _settings(args)
        with connect(settings.database_url) as conn, Embedder.from_settings(settings) as embedder:
            report = ingest(root, opts, PgStore(conn, settings.embed_model_name), embedder)

    if args.json:
        print(json.dumps({"summary": report.summary(), "actions": report.actions, "pruned": report.pruned}, ensure_ascii=False))
    else:
        if args.dry_run:
            for action, uri, detail in report.actions:
                print(f"{action:<18} {uri}  {detail}".rstrip())
        print("summary: " + ", ".join(f"{k}={v}" for k, v in report.summary().items()))
    return EXIT_ERRORS if report.counts.get("error") else 0


def cmd_search(args) -> int:
    from kb.retrieve import search
    from kb.store import connect

    settings = _settings(args)
    query_vector = None
    if not args.text_only:
        from kb.embed import Embedder, EmbeddingError

        try:
            with Embedder.from_settings(settings, max_retries=1) as embedder:
                query_vector = embedder.embed_query(args.query)
        except EmbeddingError as exc:
            log.warning("query embedding failed, falling back to full text only: %s", exc)

    with connect(settings.database_url) as conn:
        hits = search(
            conn, args.query, args.tenant, k=args.top_k, domain=args.domain,
            include_shared=args.include_shared, query_vector=query_vector,
        )
    if args.json:
        payload = {
            "query": args.query, "tenant": args.tenant, "domain": args.domain,
            "mode": "hybrid" if query_vector is not None else "text", "results": [h.to_dict() for h in hits],
        }
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0
    if not hits:
        print("(no results)")
    for i, hit in enumerate(hits, start=1):
        print(f"{i}. [{hit.score:.4f}] {hit.title or '(untitled)'} — {hit.source_uri}")
        if hit.heading_path:
            print(f"   § {hit.heading_path}")
        print(f"   {' '.join(hit.snippet.split())}\n")
    return 0


def cmd_stats(args) -> int:
    from kb.store import PgStore, connect

    settings = _settings(args)
    with connect(settings.database_url) as conn:
        stats = PgStore(conn, settings.embed_model_name).stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    header = f"{'tenant':<8} {'domain':<12} {'source':<9} {'docs':>6} {'inactive':>8} {'chunks':>7}  last update"
    print(header)
    print("-" * len(header))
    for g in stats["groups"]:
        print(
            f"{g['tenant']:<8} {g['domain'] or '-':<12} {g['source_type']:<9} {g['documents']:>6} "
            f"{g['inactive_documents']:>8} {g['chunks']:>7}  {g['last_updated'] or '-'}"
        )
    if not stats["groups"]:
        print("(empty)")
    print()
    for tenant, ts in stats["last_ingest"].items():
        print(f"last ingest [{tenant}]: {ts}")
    if not stats["last_ingest"]:
        print("last ingest: never")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from kb.service import create_app

    settings = _settings(args)  # loads .env when run on the host
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info", access_log=False)
    return 0


def cmd_maintain(args) -> int:
    from kb.memory import maintain
    from kb.store import connect

    settings = _settings(args)
    with connect(settings.database_url) as conn:
        print(json.dumps(maintain(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
