#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

TOOL = "codex-session-path-migrator"
VERSION = 2
MANIFEST = "manifest.json"
BACKUP_DIR = "session-path-migrator-backups"
WRITE_COMMANDS = {"migrate", "rollback", "cleanup"}
PATH_KEYS = {
    "cwd",
    "workspace_roots",
    "workspace-roots",
    "workspaceRoots",
    "active-workspace-roots",
    "electron-saved-workspace-roots",
    "project-order",
    "project_order",
}


def norm_basic(s: str) -> str:
    s = os.path.expandvars(os.path.expanduser(str(s).strip().strip('"')))
    s = s.replace('/', '\\')
    if s.startswith('\\\\?\\'):
        s = s[4:]
    while len(s) > 3 and s.endswith('\\'):
        s = s[:-1]
    return ntpath.normcase(ntpath.normpath(s))


def plain_path(path: str) -> str:
    raw = str(path).strip().strip('"').replace('/', '\\')
    if raw.startswith('\\\\?\\'):
        raw = raw[4:]
    while len(raw) > 3 and raw.endswith('\\'):
        raw = raw[:-1]
    return raw


def matches(value, target_path: str) -> bool:
    return isinstance(value, str) and norm_basic(value) == norm_basic(target_path)


def path_form(value: str) -> str:
    return "extended" if isinstance(value, str) and value.replace('/', '\\').startswith('\\\\?\\') else "normal"


def replacement_for(value: str, new_path: str) -> str:
    new_plain = plain_path(new_path)
    return ('\\\\?\\' + new_plain) if path_form(value) == "extended" else new_plain


def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def codex_processes():
    """Return Codex process identifiers on Windows. Conservative by design."""
    if os.name != 'nt':
        return []
    try:
        cp = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True,
            text=True,
            errors='replace',
            timeout=8,
            check=False,
        )
        found = []
        for row in csv.reader(io.StringIO(cp.stdout)):
            if len(row) < 2:
                continue
            image = row[0].strip().lower()
            if image in {'codex.exe', 'codex'}:
                found.append({'image': row[0], 'pid': row[1]})
        return found
    except Exception:
        # Failure to enumerate is not proof Codex is closed; caller applies an additional DB lock probe.
        return []


def assert_codex_closed(command: str, home: Path):
    found = codex_processes()
    if found:
        pids = ', '.join(p['pid'] for p in found)
        raise RuntimeError(
            f"SAFETY BLOCK: Codex is running (PID(s): {pids}). '{command}' is not allowed while Codex App is open. "
            "Fully exit Codex App and execute this command from an external PowerShell window."
        )

    # Secondary guard: ensure candidate SQLite databases can take an immediate write lock.
    # We immediately roll it back and change no data.
    for db, _, _ in db_specs(home):
        if not db.exists():
            continue
        conn = None
        try:
            conn = sqlite3.connect(db, timeout=0.25)
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                f"SAFETY BLOCK: could not obtain an exclusive-enough SQLite write lock on {db}: {e}. "
                "Codex or another process may still be using the database."
            )
        finally:
            if conn is not None:
                conn.close()


def key_is_path_bearing(key) -> bool:
    return isinstance(key, str) and key in PATH_KEYS


def json_transform(obj, old_path, new_path, key=None):
    changed = 0
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            new_key = k
            if key_is_path_bearing(key) and isinstance(k, str) and matches(k, old_path):
                new_key = replacement_for(k, new_path)
                changed += 1
            nv, c = json_transform(v, old_path, new_path, k)
            out[new_key] = nv
            changed += c
        return out, changed
    if isinstance(obj, list):
        out = []
        for v in obj:
            nv, c = json_transform(v, old_path, new_path, key)
            out.append(nv)
            changed += c
        return out, changed
    if isinstance(obj, str) and key_is_path_bearing(key) and matches(obj, old_path):
        return replacement_for(obj, new_path), 1
    return obj, 0


def collect_json_matches(obj, target_path, key=None, forms=None):
    count = 0
    if forms is None:
        forms = {'normal': 0, 'extended': 0}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_is_path_bearing(key) and isinstance(k, str) and matches(k, target_path):
                count += 1
                forms[path_form(k)] += 1
            count += collect_json_matches(v, target_path, k, forms)
    elif isinstance(obj, list):
        for v in obj:
            count += collect_json_matches(v, target_path, key, forms)
    elif isinstance(obj, str) and key_is_path_bearing(key) and matches(obj, target_path):
        count += 1
        forms[path_form(obj)] += 1
    return count


def iter_text_candidates(home: Path):
    for rel in [Path('.codex-global-state.json'), Path('session_index.jsonl')]:
        p = home / rel
        if p.exists():
            yield p
    for base_name in ('sessions', 'archived_sessions'):
        base = home / base_name
        if base.exists():
            yield from base.rglob('*.jsonl')


def inspect_text_file(path: Path, target_path: str):
    count = 0
    forms = {'normal': 0, 'extended': 0}
    try:
        if path.suffix.lower() == '.json' and path.name != 'session_index.jsonl':
            obj = json.loads(path.read_text(encoding='utf-8'))
            count = collect_json_matches(obj, target_path, forms=forms)
            return count, forms
        with path.open('r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += collect_json_matches(obj, target_path, forms=forms)
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return count, forms


def update_text_file(path: Path, old_path: str, new_path: str):
    if path.suffix.lower() == '.json' and path.name != 'session_index.jsonl':
        obj = json.loads(path.read_text(encoding='utf-8'))
        new_obj, changed = json_transform(obj, old_path, new_path)
        if changed:
            path.write_text(json.dumps(new_obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return changed

    lines = []
    changed_total = 0
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                lines.append(line)
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                lines.append(line)
                continue
            new_obj, changed = json_transform(obj, old_path, new_path)
            changed_total += changed
            if changed:
                lines.append(json.dumps(new_obj, ensure_ascii=False, separators=(',', ':')) + '\n')
            else:
                lines.append(line)
    if changed_total:
        with path.open('w', encoding='utf-8', newline='') as f:
            f.writelines(lines)
    return changed_total


def db_specs(home: Path):
    return [
        (home / 'state_5.sqlite', 'threads', 'cwd'),
        (home / 'sqlite' / 'codex-dev.db', 'local_thread_catalog', 'cwd'),
    ]


def table_column_exists(conn, table, col):
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return any(r[1] == col for r in rows)
    except sqlite3.Error:
        return False


def db_values(path: Path, table: str, col: str):
    if not path.exists():
        return []
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2)
    try:
        if not table_column_exists(conn, table, col):
            return []
        return [r[0] for r in conn.execute(f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL')]
    finally:
        conn.close()


def db_counts(path: Path, table: str, col: str, old_path: str, new_path: str = None):
    vals = db_values(path, table, col)
    old_vals = [v for v in vals if matches(v, old_path)]
    new_vals = [v for v in vals if new_path and matches(v, new_path)]
    forms = {'normal': 0, 'extended': 0}
    for v in old_vals:
        forms[path_form(v)] += 1
    return len(old_vals), len(new_vals), forms


def db_update(path: Path, table: str, col: str, old_path: str, new_path: str):
    if not path.exists():
        return 0
    conn = sqlite3.connect(path, timeout=5)
    try:
        if not table_column_exists(conn, table, col):
            return 0
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute(f'SELECT rowid, "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL').fetchall()
        changes = [(replacement_for(v, new_path), rowid) for rowid, v in rows if matches(v, old_path)]
        if changes:
            conn.executemany(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?', changes)
        conn.commit()
        return len(changes)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def affected_files(home: Path, old_path: str, new_path: str):
    files = []
    db_rows = 0
    db_details = []
    forms = {'normal': 0, 'extended': 0}
    for p, t, c in db_specs(home):
        oldc, newc, f = db_counts(p, t, c, old_path, new_path)
        forms['normal'] += f['normal']
        forms['extended'] += f['extended']
        if p.exists():
            db_details.append((p, t, c, oldc, newc))
        if oldc:
            db_rows += oldc
            files.append(p)

    text_matches = 0
    text_details = []
    for p in iter_text_candidates(home):
        count, f = inspect_text_file(p, old_path)
        forms['normal'] += f['normal']
        forms['extended'] += f['extended']
        if count:
            text_matches += count
            files.append(p)
            text_details.append((p, count))

    return {
        'files': sorted(set(files)),
        'db_rows': db_rows,
        'db_details': db_details,
        'text_matches': text_matches,
        'text_details': text_details,
        'forms': forms,
    }


def make_backup(home: Path, files, old_path: str, new_path: str):
    backup_root = home / BACKUP_DIR
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    dst = backup_root / backup_id
    dst.mkdir(parents=True)
    entries = []
    for src in sorted(set(files)):
        rel = src.relative_to(home)
        out = dst / 'files' / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        entries.append({
            'relative_path': str(rel).replace('\\', '/'),
            'sha256': sha256(out),
            'size': out.stat().st_size,
        })
    manifest = {
        'tool': TOOL,
        'version': VERSION,
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'codex_home': str(home),
        'old_path': old_path,
        'new_path': new_path,
        'files': entries,
    }
    (dst / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return backup_id, dst


def load_manifest(home: Path, backup_id: str):
    if not backup_id or any(x in backup_id for x in ('/', '\\', '..')):
        raise RuntimeError('Invalid BackupId.')
    root = (home / BACKUP_DIR / backup_id).resolve()
    expected_parent = (home / BACKUP_DIR).resolve()
    if root.parent != expected_parent:
        raise RuntimeError('Backup path escapes the managed backup directory.')
    mp = root / MANIFEST
    if not mp.exists():
        raise RuntimeError('Backup manifest not found.')
    manifest = json.loads(mp.read_text(encoding='utf-8'))
    if manifest.get('tool') != TOOL:
        raise RuntimeError('Refusing to operate on a backup not created by this tool.')
    if norm_basic(manifest.get('codex_home', '')) != norm_basic(str(home)):
        raise RuntimeError('Backup belongs to a different Codex home; refusing operation.')
    return root, manifest


def print_inspection(home: Path, old_path: str, new_path: str, info):
    print(f'Codex home: {home}')
    print(f'Old path:   {old_path}')
    print(f'New path:   {new_path}')
    print('\nDatabase matches:')
    if not info['db_details']:
        print('  (no known database files found)')
    for p, t, c, oldc, newc in info['db_details']:
        print(f'  {p.relative_to(home)} :: {t}.{c}: old={oldc}, new={newc}')
    print('\nText/session matches:')
    if not info['text_details']:
        print('  (none)')
    for p, count in info['text_details']:
        print(f'  {p.relative_to(home)}: {count}')
    print('\nPath forms among old-path matches:')
    print(f"  normal:   {info['forms']['normal']}")
    print(f"  extended: {info['forms']['extended']}  (\\\\?\\ prefix)")
    print('\nFiles that would be backed up and modified:')
    if not info['files']:
        print('  (none)')
    for p in info['files']:
        print(f'  {p.relative_to(home)}')
    print(
        f"\nSummary: affected DB rows={info['db_rows']}; targeted text/session values={info['text_matches']}; "
        f"affected files={len(info['files'])}."
    )


def cmd_inspect(home, old_path, new_path):
    info = affected_files(home, old_path, new_path)
    print_inspection(home, old_path, new_path, info)
    return 0


def cmd_prepare(home, old_path, new_path):
    info = affected_files(home, old_path, new_path)
    print_inspection(home, old_path, new_path, info)
    if not info['files']:
        print('\nNo targeted references to the old path were found. No migration command is recommended.')
        return 2
    wrapper = Path(__file__).with_name('migrate.ps1').resolve()
    print('\nREAD-ONLY PREPARATION COMPLETE.')
    print('Do not run the migration command inside the running Codex App.')
    print('Fully exit Codex App, open an external PowerShell window, then run:')
    print()
    print(f"& {ps_quote(str(wrapper))} migrate -OldPath {ps_quote(old_path)} -NewPath {ps_quote(new_path)}")
    print('\nAfter migration, local verification command:')
    print(f"& {ps_quote(str(wrapper))} verify -OldPath {ps_quote(old_path)} -NewPath {ps_quote(new_path)}")
    return 0


def cmd_migrate(home, old_path, new_path):
    assert_codex_closed('migrate', home)
    if norm_basic(old_path) == norm_basic(new_path):
        raise RuntimeError('OldPath and NewPath resolve to the same path.')
    info = affected_files(home, old_path, new_path)
    if not info['files']:
        print('No targeted references to the old path were found. Nothing changed.')
        return 2

    backup_id, backup_path = make_backup(home, info['files'], old_path, new_path)
    db_changes = 0
    text_changes = 0
    try:
        for p, t, c in db_specs(home):
            db_changes += db_update(p, t, c, old_path, new_path)
        for p in iter_text_candidates(home):
            count, _ = inspect_text_file(p, old_path)
            if count:
                text_changes += update_text_file(p, old_path, new_path)
    except Exception:
        print('Migration failed after backup creation. Keep Codex closed and roll back using the Backup ID below.', file=sys.stderr)
        print(f'Backup ID: {backup_id}', file=sys.stderr)
        raise

    print(f'Migration complete. Database rows changed: {db_changes}; targeted text/session values changed: {text_changes}.')
    print(f'Backup ID: {backup_id}')
    print(f'Backup path: {backup_path}')
    print('\nNext: run verify, reopen Codex App, and visually confirm the old tasks appear under the new project path.')
    print('Do NOT delete this backup until that UI check succeeds.')
    return 0


def cmd_verify(home, old_path, new_path):
    info = affected_files(home, old_path, new_path)
    print('Verification results:')
    for p, t, c in db_specs(home):
        oldc, newc, _ = db_counts(p, t, c, old_path, new_path)
        if p.exists():
            print(f'  DB {p.relative_to(home)} :: {t}.{c}: old={oldc}, new={newc}')
    print(f"  Targeted old-path text/session values remaining: {info['text_matches']}")
    old_remaining = info['db_rows'] + info['text_matches']
    if old_remaining:
        print(f'FAILED: {old_remaining} targeted old-path reference(s) remain.', file=sys.stderr)
        return 3

    new_db_total = 0
    for p, t, c in db_specs(home):
        _, newc, _ = db_counts(p, t, c, old_path, new_path)
        new_db_total += newc
    if new_db_total == 0:
        print('WARNING: no known database cwd rows match the new path. Review the migration before accepting it.', file=sys.stderr)
        return 4
    print('LOCAL VERIFY PASSED.')
    print('Final acceptance still requires reopening Codex App and visually confirming the expected tasks are under the new project.')
    return 0


def cmd_rollback(home, backup_id, confirmed):
    assert_codex_closed('rollback', home)
    if not confirmed:
        raise RuntimeError('Rollback confirmation missing.')
    root, manifest = load_manifest(home, backup_id)
    for item in manifest['files']:
        rel = Path(item['relative_path'])
        src = root / 'files' / rel
        dst = home / rel
        if not src.exists() or sha256(src) != item['sha256']:
            raise RuntimeError(f'Backup integrity check failed for {rel}')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(f'Rollback complete from backup {backup_id}.')
    print('The backup was retained. Reopen Codex and verify recovery; after success, close Codex and run cleanup.')
    return 0


def cmd_cleanup(home, backup_id, confirmed):
    assert_codex_closed('cleanup', home)
    if not confirmed:
        raise RuntimeError('Backup deletion confirmation missing.')
    root, manifest = load_manifest(home, backup_id)
    for item in manifest['files']:
        p = root / 'files' / Path(item['relative_path'])
        if not p.exists() or sha256(p) != item['sha256']:
            raise RuntimeError(f'Backup integrity check failed for {item["relative_path"]}; refusing deletion.')
    shutil.rmtree(root)
    print(f'Deleted migration backup: {backup_id}')
    parent = home / BACKUP_DIR
    try:
        next(parent.iterdir())
    except StopIteration:
        parent.rmdir()
    except FileNotFoundError:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser(description=TOOL)
    ap.add_argument('command', choices=['inspect', 'prepare', 'migrate', 'verify', 'rollback', 'cleanup'])
    ap.add_argument('--codex-home', required=True)
    ap.add_argument('--old-path')
    ap.add_argument('--new-path')
    ap.add_argument('--backup-id')
    ap.add_argument('--confirm-rollback', action='store_true')
    ap.add_argument('--confirm-backup-deletion', action='store_true')
    args = ap.parse_args()

    home = Path(args.codex_home).expanduser().resolve()
    if args.command in {'inspect', 'prepare', 'migrate', 'verify'}:
        if not args.old_path or not args.new_path:
            ap.error('--old-path and --new-path are required for this command')
    if args.command in {'rollback', 'cleanup'} and not args.backup_id:
        ap.error('--backup-id is required for this command')

    try:
        if args.command == 'inspect':
            return cmd_inspect(home, args.old_path, args.new_path)
        if args.command == 'prepare':
            return cmd_prepare(home, args.old_path, args.new_path)
        if args.command == 'migrate':
            return cmd_migrate(home, args.old_path, args.new_path)
        if args.command == 'verify':
            return cmd_verify(home, args.old_path, args.new_path)
        if args.command == 'rollback':
            return cmd_rollback(home, args.backup_id, args.confirm_rollback)
        if args.command == 'cleanup':
            return cmd_cleanup(home, args.backup_id, args.confirm_backup_deletion)
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
