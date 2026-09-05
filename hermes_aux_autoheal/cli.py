"""Command line entry point: ``hermes-aux-autoheal``.

Default behaviour is a DRY RUN. Nothing that rewrites a user's config should do
so on a bare invocation with no arguments — you ask for the write with
``--apply``.

Exit codes are meant for cron and monitoring:

* 0 — route is correct, or was corrected
* 1 — nothing healthy to route to (config left untouched)
* 2 — a write was attempted and refused (lock contention, conflict, validation)

Stdout is SILENT on a no-change run so a cron entry only speaks when something
happened. ``--verbose`` shows every candidate and its verdict.
"""
import argparse
import os
import sys
import time

from . import config_io, context, discovery, health, router


def default_home():
    return os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))


def build_parser():
    p = argparse.ArgumentParser(
        prog='hermes-aux-autoheal',
        description='Keep a Hermes auxiliary task routed to models that answer.')
    p.add_argument('--task', default='compression',
                   help='auxiliary task to heal (default: compression; '
                        '"vision" probes with a real image so text-only '
                        'models are never routed for it)')
    p.add_argument('--apply', action='store_true',
                   help='write the route (default is a dry run)')
    p.add_argument('--verbose', '-v', action='store_true',
                   help='print every candidate and its verdict')
    p.add_argument('--config',
                   help='path to config.yaml (default: $HERMES_HOME/config.yaml)')
    p.add_argument('--env-file',
                   help='path to .env for API keys (default: $HERMES_HOME/.env)')
    p.add_argument('--sqlite-db',
                   help='optional dashboard SQLite db to also read providers from')
    p.add_argument('--no-discover-models', action='store_true',
                   help='do not ask providers for their /v1/models listing; '
                        'use only models pinned in config.yaml')
    p.add_argument('--max-discovered', type=int,
                   default=discovery.DEFAULT_MAX_DISCOVERED,
                   help='cap on models taken from one provider listing '
                        f'(default: {discovery.DEFAULT_MAX_DISCOVERED})')
    p.add_argument('--chain-depth', type=int, default=router.DEFAULT_CHAIN_DEPTH,
                   help=f'fallback entries to keep (default: {router.DEFAULT_CHAIN_DEPTH})')
    p.add_argument('--chat-depth', type=int, default=router.DEFAULT_CHAT_CHAIN_DEPTH,
                   help='entries to keep in the CHAT model\'s fallback_providers '
                        f'(default: {router.DEFAULT_CHAT_CHAIN_DEPTH})')
    p.add_argument('--no-chat-chain', action='store_true',
                   help='do not touch the top-level fallback_providers at all '
                        '(use when another process already maintains it)')
    p.add_argument('--call-timeout', type=int, default=router.DEFAULT_CALL_TIMEOUT,
                   help='per-entry timeout written into the route, seconds')
    p.add_argument('--probe-timeout', type=float,
                   default=health.DEFAULT_PROBE_TIMEOUT,
                   help='health probe timeout, seconds')
    p.add_argument('--min-context', type=int, default=0,
                   help='skip models whose known context window is below this')
    p.add_argument('--ttl', type=int, default=health.DEFAULT_TTL,
                   help='reuse cached probe results for this many seconds')
    p.add_argument('--demote-streak', type=int, default=health.DEFAULT_DEMOTE_STREAK,
                   help='ambiguous failures before a model leaves the route')
    p.add_argument('--promote-streak', type=int, default=health.DEFAULT_PROMOTE_STREAK,
                   help='passes before a down model is trusted again')
    p.add_argument('--sticky-rel', type=float, default=router.DEFAULT_STICKY_REL,
                   help='fraction faster a challenger must be to displace a '
                        'model already in the route (0 disables)')
    p.add_argument('--sticky-abs', type=float, default=router.DEFAULT_STICKY_ABS,
                   help='seconds faster a challenger must also be, in absolute '
                        'terms (0 disables)')
    p.add_argument('--latency-window', type=int, default=health.DEFAULT_LATENCY_WINDOW,
                   help='rank on the median of this many recent probes rather '
                        'than the latest one (1 disables smoothing)')
    p.add_argument('--cache',
                   help='health cache path (default: $HERMES_HOME/.aux_autoheal_health.json)')
    p.add_argument('--no-cache', action='store_true',
                   help='probe everything, ignoring cached results')
    p.add_argument('--hermes-path',
                   help="path to the Hermes package, for context windows "
                        "(default: $HERMES_PACKAGE)")
    p.add_argument('--no-context-lookup', action='store_true',
                   help='skip context-window resolution entirely')
    p.add_argument('--fast-pattern',
                   help='regex marking a model name as cheap/fast (ranks first)')
    p.add_argument('--heavy-pattern',
                   help='regex marking a model name as heavy (ranks last)')
    p.add_argument('--prune-backups', action='store_true',
                   help='also trim this tool\'s own config backup history')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    home = default_home()
    config_path = args.config or os.environ.get(
        'HERMES_CONFIG', f'{home}/config.yaml')
    env_file = args.env_file or os.environ.get(
        'HERMES_ENV_FILE', f'{home}/.env')
    cache_path = args.cache or f'{home}/.aux_autoheal_health.json'

    if not os.path.exists(config_path):
        print(f'ERROR config not found: {config_path}', file=sys.stderr)
        return 2

    if args.fast_pattern or args.heavy_pattern:
        import re
        try:
            router.set_patterns(args.fast_pattern, args.heavy_pattern)
        except re.error as exc:
            print(f'ERROR bad tier pattern: {exc}', file=sys.stderr)
            return 2

    config_io.CONFIG_PATH = config_path

    def emit(msg):
        print(msg)

    def vprint(msg):
        if args.verbose:
            print(msg)

    with open(config_path) as f:
        config, _ = config_io.parse(f.read())

    candidates, skipped = discovery.discover(
        config, sqlite_db=args.sqlite_db, env_file=env_file,
        discover_models=not args.no_discover_models,
        max_discovered=args.max_discovered)
    for cand, why in skipped:
        vprint(f'  skip {cand["provider"]}/{cand["model"]}: {why}')

    if not candidates:
        emit('ERROR no candidate models with usable API keys — '
             'check custom_providers and your .env')
        return 1

    cache = health.HealthCache(cache_path,
                               ttl=0 if args.no_cache else args.ttl)
    ctx_lookup = None
    if not args.no_context_lookup:
        ctx_lookup = context.make_lookup(hermes_path=args.hermes_path)
    eligible, rejected = health.evaluate(
        candidates, cache,
        timeout=args.probe_timeout,
        demote_streak=args.demote_streak,
        promote_streak=args.promote_streak,
        latency_window=args.latency_window,
        context_lookup=ctx_lookup,
        task=args.task)
    cache.save()

    for cand, why in rejected:
        vprint(f'  skip {cand["provider"]}/{cand["model"]}: {why}')

    aux = config.get('auxiliary')
    current = (aux or {}).get(args.task)
    incumbents = router.route_idents(current)
    incumbent_primary = router.primary_ident(current)
    incumbent_chain = router.chain_entries(current)

    for c in router.rank(eligible, incumbents,
                         sticky_rel=args.sticky_rel, sticky_abs=args.sticky_abs):
        grace = '' if c['ok_now'] else f' GRACE(strike {c["fail_streak"]}/{args.demote_streak})'
        held = ' HELD' if (c['provider'], c['model']) in incumbents else ''
        med = c.get('lat_median', c.get('latency', 99.0))
        n = c.get('lat_n', 0)
        ctx = f'{c["context"]:,}' if c.get('context') else 'unknown'
        vprint(f'  ok   {c["provider"]}/{c["model"]}: '
               f'tier={router.tier_of(c["model"])} ctx={ctx} '
               f'probe={c["latency"]:.1f}s med={med:.1f}s(n={n}){held}{grace}')

    desired = router.build(
        eligible,
        chain_depth=args.chain_depth,
        call_timeout=args.call_timeout,
        min_context=args.min_context or None,
        incumbents=incumbents,
        incumbent_primary=incumbent_primary,
        incumbent_chain=incumbent_chain,
        sticky_rel=args.sticky_rel,
        sticky_abs=args.sticky_abs)

    if desired is None:
        emit('ERROR no healthy candidate — leaving config untouched')
        return 1

    changed, reason = router.needs_write(current, desired)

    # --- the CHAT model's own fallback list (top-level fallback_providers) ---
    # model.provider/model.default are the user's choice and are NEVER written
    # here; this only maintains the spares behind them. Same probe results, same
    # stickiness guard, different ranking (chat_slot_key, not tier_of).
    chat_changed, chat_reason = False, ''
    chat_desc = None
    chat_desired = None
    model_cfg = config.get('model') or {}
    chat_primary = (model_cfg.get('provider'), model_cfg.get('default'))
    if chat_primary[0] and chat_primary[1] and not args.no_chat_chain:
        chat_current = config.get('fallback_providers')
        chat_incumbent = tuple(chat_current) if isinstance(chat_current, list) else ()
        chat_chain = router.pick_chat_chain(
            eligible, chat_primary,
            depth=args.chat_depth,
            incumbent_chain=chat_incumbent,
            all_candidates=candidates)
        chat_desc = [(c['provider'], c['model']) for c in chat_chain]
        chat_desired = [router.as_chat_entry(c) for c in chat_chain]
        chat_changed, chat_reason = router.chat_chain_needs_write(
            chat_current, chat_desired)
    else:
        vprint('skip chat chain: model.provider / model.default not set')

    chain_desc = [(e['provider'], e['model']) for e in desired['fallback_chain']]
    if not changed and not chat_changed:
        vprint(f'route already correct: {desired["provider"]}/{desired["model"]} '
               f'+ {len(chain_desc)} fallback(s)')
        if chat_desc is not None:
            vprint(f'chat chain already correct: {len(chat_desc)} fallback(s)')
        return 0

    plan = (f'{args.task}: primary={desired["provider"]}/{desired["model"]}, '
            f'chain={chain_desc} ({reason})')

    if chat_changed:
        plan += (f'\n  chat fallback_providers={chat_desc} ({chat_reason})')

    if not args.apply:
        emit(f'DRY RUN would update {plan}')
        emit('re-run with --apply to write it')
        return 0

    try:
        with config_io.config_transaction(backup_ns='auxautoheal',
                                         path=config_path) as tx:
            aux = tx.doc.setdefault('auxiliary', {})
            task_cfg = aux.setdefault(args.task, {})
            task_cfg['provider'] = desired['provider']
            task_cfg['model'] = desired['model']
            task_cfg['timeout'] = desired['timeout']
            config_io.replace_seq(task_cfg, 'fallback_chain',
                                  desired['fallback_chain'])
            if chat_changed:
                config_io.replace_seq(tx.doc, 'fallback_providers',
                                      chat_desired)
    except config_io.ConfigConflict as exc:
        emit(f'SKIP another writer holds config.yaml ({exc})')
        return 2
    except config_io.ConfigInvalid as exc:
        emit(f'REFUSED render failed validation ({exc}) — config untouched')
        return 2

    if args.prune_backups:
        config_io.prune_backups('auxautoheal')

    stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    emit(f'[{stamp}] route updated {plan}')
    if router.should_notify(reason, desired):
        emit('  ^ primary changed or chain nearly empty — worth a look')
    return 0


if __name__ == '__main__':
    sys.exit(main())
