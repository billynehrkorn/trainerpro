"""Client portal: account management, client auth, and all /client-portal routes.

This module keeps endpoint names identical to when they lived in app.py
(no blueprint prefix), so templates using url_for('client_portal'),
url_for('client_login'), etc. keep working without any changes.

Call register_client_routes(app) from app.py to attach all routes.
The two table-setup helpers are module-level so app.py can import them
for startup initialization.
"""
from flask import (
    render_template, request, redirect, url_for, flash, session, jsonify
)
from datetime import datetime
import uuid
import json

from db import get_db
from auth_utils import login_required, client_login_required



def init_client_accounts_table():
    """Create client_accounts table if it doesn't exist. Call at startup."""
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS client_accounts (
            id            TEXT PRIMARY KEY,
            client_id     TEXT NOT NULL,
            access_code   TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            is_active     INTEGER DEFAULT 1,
            theme         TEXT DEFAULT 'light',
            created_at    TIMESTAMP,
            last_login    TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        )
    ''')
    # Safely add columns that may not exist in older DBs
    migrations = [
        "ALTER TABLE client_accounts ADD COLUMN theme TEXT DEFAULT 'light'",
        "ALTER TABLE client_accounts ADD COLUMN perm_workouts INTEGER DEFAULT 1",
        "ALTER TABLE client_accounts ADD COLUMN perm_weight INTEGER DEFAULT 1",
        "ALTER TABLE client_accounts ADD COLUMN perm_nutrition INTEGER DEFAULT 1",
        "ALTER TABLE client_accounts ADD COLUMN perm_sleep INTEGER DEFAULT 1",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass  # Column already exists
    conn.commit()
    conn.close()

def backfill_client_access_codes():
    """Auto-generate portal access codes for any existing client without one."""
    import random, string

    def make_code(conn):
        chars = string.ascii_uppercase + string.digits
        while True:
            code = f"{''.join(random.choices(chars, k=4))}-{''.join(random.choices(chars, k=4))}"
            if not conn.execute('SELECT id FROM client_accounts WHERE access_code = ?', (code,)).fetchone():
                return code

    conn = get_db()
    clients_without_code = conn.execute('''
        SELECT c.id FROM clients c
        LEFT JOIN client_accounts ca ON ca.client_id = c.id
        WHERE ca.id IS NULL
    ''').fetchall()

    for row in clients_without_code:
        code = make_code(conn)
        conn.execute('''
            INSERT INTO client_accounts (id, client_id, access_code, is_active, created_at)
            VALUES (?, ?, ?, 1, ?)
        ''', (str(uuid.uuid4()), row['id'], code, datetime.now()))

    conn.commit()
    conn.close()
    print(f"[portal] Backfilled {len(clients_without_code)} client access codes")





def init_nutrition_protein_column():
    """Add the estimated_protein column to nutrition_logs if it is missing.

    Safe to run on every startup: the ALTER TABLE is wrapped so an
    already-existing column (OperationalError) is simply ignored.
    """
    conn = get_db()
    try:
        conn.execute('ALTER TABLE nutrition_logs ADD COLUMN estimated_protein REAL')
        conn.commit()
        print('[nutrition] added estimated_protein column')
    except Exception:
        # Column already exists — nothing to do.
        pass
    finally:
        conn.close()

def init_activity_log_table():
    """Create the activity_log table that records client portal actions.

    Each row is one event the trainer should see in their activity stream:
    a client creating/editing/deleting a workout, weight, sleep, or nutrition entry.
    """
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS activity_log (
            id           TEXT PRIMARY KEY,
            trainer_id   TEXT NOT NULL,
            client_id    TEXT NOT NULL,
            client_name  TEXT,
            category     TEXT NOT NULL,
            action       TEXT NOT NULL,
            detail       TEXT,
            created_at   TIMESTAMP NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def log_activity(conn, client_id, category, action, detail=''):
    """Insert one activity-stream event using the caller's open connection so it
    shares the same transaction (the calling route commits).

    Looks up trainer_id + client name from the clients table so the trainer's
    stream can be filtered and labelled cheaply at read time. Never raises —
    activity logging must not break the underlying action.
    """
    try:
        row = conn.execute(
            'SELECT trainer_id, name FROM clients WHERE id = ?',
            (client_id,)
        ).fetchone()
        if not row:
            return
        # Support both sqlite3.Row (by-name) and plain tuple rows.
        try:
            trainer_id = row['trainer_id']
            client_name = row['name']
        except (TypeError, IndexError):
            trainer_id = row[0]
            client_name = row[1]
        if not trainer_id:
            return
        conn.execute('''
            INSERT INTO activity_log
            (id, trainer_id, client_id, client_name, category, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (str(uuid.uuid4()), trainer_id, client_id, client_name,
              category, action, detail, datetime.utcnow()))
    except Exception as e:
        print(f"[activity] log_activity failed: {e}")


def register_client_routes(app):
    """Attach all client-portal routes to the given Flask app."""

    @app.route('/api/client-theme', methods=['POST'])
    @client_login_required
    def update_client_theme():
        data = request.get_json(silent=True) or {}
        theme = data.get('theme')
        if theme not in ('light', 'dark'):
            return jsonify({'error': 'Invalid theme'}), 400
        conn = get_db()
        conn.execute(
            'UPDATE client_accounts SET theme = ? WHERE id = ?',
            (theme, session['client_account_id'])
        )
        conn.commit()
        conn.close()
        session['client_theme'] = theme
        return jsonify({'success': True, 'theme': theme})

    # ── Generate access code (trainer action) ──────────────────────────────────

    @app.route('/api/clients/<client_id>/generate-access-code', methods=['POST'])
    @login_required
    def generate_access_code(client_id):
        """Trainer generates a new access code for a client."""
        import random, string

        conn = get_db()

        # Make sure the client belongs to this trainer
        client = conn.execute(
            'SELECT id FROM clients WHERE id = ? AND trainer_id = ?',
            (client_id, session['user_id'])
        ).fetchone()

        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404

        # Generate a readable 8-char code like FX7K-P2QR
        def make_code():
            chars = string.ascii_uppercase + string.digits
            part1 = ''.join(random.choices(chars, k=4))
            part2 = ''.join(random.choices(chars, k=4))
            return f"{part1}-{part2}"

        # Ensure uniqueness
        code = make_code()
        while conn.execute('SELECT id FROM client_accounts WHERE access_code = ?', (code,)).fetchone():
            code = make_code()

        # Check if account already exists — if so, update the code and re-activate
        existing = conn.execute(
            'SELECT id FROM client_accounts WHERE client_id = ?', (client_id,)
        ).fetchone()

        if existing:
            conn.execute('''
                UPDATE client_accounts
                SET access_code = ?, password_hash = NULL, is_active = 1, created_at = ?
                WHERE client_id = ?
            ''', (code, datetime.now(), client_id))
        else:
            conn.execute('''
                INSERT INTO client_accounts (id, client_id, access_code, is_active, created_at)
                VALUES (?, ?, ?, 1, ?)
            ''', (str(uuid.uuid4()), client_id, code, datetime.now()))

        conn.commit()
        conn.close()
        return jsonify({'success': True, 'access_code': code})


    @app.route('/api/clients/<client_id>/revoke-access', methods=['POST'])
    @login_required
    def revoke_client_access(client_id):
        """Trainer revokes a client's portal access."""
        conn = get_db()
        client = conn.execute(
            'SELECT id FROM clients WHERE id = ? AND trainer_id = ?',
            (client_id, session['user_id'])
        ).fetchone()

        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404

        conn.execute(
            'UPDATE client_accounts SET is_active = 0 WHERE client_id = ?',
            (client_id,)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})


    @app.route('/api/clients/<client_id>/portal-settings', methods=['GET'])
    @login_required
    def get_portal_settings(client_id):
        conn = get_db()
        client = conn.execute(
            'SELECT id FROM clients WHERE id = ? AND trainer_id = ?',
            (client_id, session['user_id'])
        ).fetchone()
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404
        account = conn.execute(
            'SELECT is_active, perm_workouts, perm_weight, perm_nutrition, perm_sleep FROM client_accounts WHERE client_id = ?',
            (client_id,)
        ).fetchone()
        conn.close()
        if not account:
            return jsonify({'error': 'No portal account found'}), 404
        return jsonify({
            'access':    bool(account['is_active']),
            'workouts':  bool(account['perm_workouts']),
            'weight':    bool(account['perm_weight']),
            'nutrition': bool(account['perm_nutrition']),
            'sleep':     bool(account['perm_sleep']),
        })


    @app.route('/api/clients/<client_id>/portal-settings', methods=['POST'])
    @login_required
    def save_portal_settings(client_id):
        conn = get_db()
        client = conn.execute(
            'SELECT id FROM clients WHERE id = ? AND trainer_id = ?',
            (client_id, session['user_id'])
        ).fetchone()
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404
        data = request.get_json()
        conn.execute('''
            UPDATE client_accounts
            SET is_active      = ?,
                perm_workouts  = ?,
                perm_weight    = ?,
                perm_nutrition = ?,
                perm_sleep     = ?
            WHERE client_id = ?
        ''', (
            1 if data.get('access')    else 0,
            1 if data.get('workouts')  else 0,
            1 if data.get('weight')    else 0,
            1 if data.get('nutrition') else 0,
            1 if data.get('sleep')     else 0,
            client_id
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True})


    @app.route('/api/clients/<client_id>/portal-status')
    @login_required
    def client_portal_status(client_id):
        """Return current portal access status for a client."""
        conn = get_db()
        client = conn.execute(
            'SELECT id FROM clients WHERE id = ? AND trainer_id = ?',
            (client_id, session['user_id'])
        ).fetchone()

        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404

        account = conn.execute(
            'SELECT access_code, is_active, password_hash, last_login FROM client_accounts WHERE client_id = ?',
            (client_id,)
        ).fetchone()
        conn.close()

        if not account:
            return jsonify({'has_account': False})

        return jsonify({
            'has_account': True,
            'access_code': account['access_code'] if account['is_active'] else None,
            'is_active': bool(account['is_active']),
            'has_password': bool(account['password_hash']),
            'last_login': account['last_login']
        })


    # ── Client login ───────────────────────────────────────────────────────────

    @app.route('/client-login', methods=['GET', 'POST'])
    def client_login():
        """Handles both client login and first-time registration."""
        if request.method == 'POST':
            action      = request.form.get('action', 'login')
            access_code = request.form.get('access_code', '').strip().upper()
            password    = request.form.get('password', '').strip()

            conn = get_db()
            account = conn.execute('''
                SELECT ca.*, c.name AS client_name, c.id AS client_id
                FROM client_accounts ca
                JOIN clients c ON ca.client_id = c.id
                WHERE ca.access_code = ? AND ca.is_active = 1
            ''', (access_code,)).fetchone()

            if not account:
                conn.close()
                flash('Invalid access code or access has been revoked.', 'client_error')
                return redirect(url_for('login'))

            # ── Register (first-time setup) ────────────────────────────────
            if action == 'register':
                if account['password_hash']:
                    conn.close()
                    flash('This account already has a password. Please sign in instead.', 'client_register_error')
                    return redirect(url_for('login'))

                confirm = request.form.get('confirm_password', '').strip()
                if len(password) < 6:
                    conn.close()
                    flash('Password must be at least 6 characters.', 'client_register_error')
                    return redirect(url_for('login'))
                if password != confirm:
                    conn.close()
                    flash('Passwords do not match.', 'client_register_error')
                    return redirect(url_for('login'))

                # Store password as plain text (no hashing)
                conn.execute(
                    'UPDATE client_accounts SET password_hash = ?, last_login = ? WHERE id = ?',
                    (password, datetime.now(), account['id'])
                )
                conn.commit()
                conn.close()

                session['client_account_id'] = account['id']
                session['client_id']          = account['client_id']
                session['client_name']        = account['client_name']
                session['account_type']       = 'client'
                session['client_theme'] = account['theme'] or 'light'
                return redirect(url_for('client_portal'))

            # ── Login (returning user) ─────────────────────────────────────
            if not account['password_hash']:
                conn.close()
                flash('No password set yet. Please use the Register tab to create one.', 'client_error')
                return redirect(url_for('login'))

            # Plain text password comparison (no hashing)
            if account['password_hash'] != password:
                conn.close()
                flash('Incorrect password.', 'client_error')
                return redirect(url_for('login'))

            conn.execute(
                'UPDATE client_accounts SET last_login = ? WHERE id = ?',
                (datetime.now(), account['id'])
            )
            conn.commit()
            conn.close()

            session['client_account_id'] = account['id']
            session['client_id']          = account['client_id']
            session['client_name']        = account['client_name']
            session['account_type']       = 'client'
            session['client_theme'] = account['theme'] or 'light'
            return redirect(url_for('client_portal'))

        return redirect(url_for('login'))


    @app.route('/client-set-password', methods=['GET', 'POST'])
    def client_set_password():
        """First-time client sets their own password."""
        if 'pending_client_code' not in session:
            return redirect(url_for('login'))

        if request.method == 'POST':
            password = request.form.get('password', '').strip()
            confirm  = request.form.get('confirm_password', '').strip()

            if len(password) < 6:
                flash('Password must be at least 6 characters.')
                return render_template('client/set_password.html')

            if password != confirm:
                flash('Passwords do not match.')
                return render_template('client/set_password.html')

            access_code = session.pop('pending_client_code')
            conn = get_db()
            account = conn.execute('''
                SELECT ca.*, c.name AS client_name, c.id AS client_id
                FROM client_accounts ca
                JOIN clients c ON ca.client_id = c.id
                WHERE ca.access_code = ? AND ca.is_active = 1
            ''', (access_code,)).fetchone()

            if not account:
                conn.close()
                flash('Access code is no longer valid.')
                return redirect(url_for('login'))

            conn.execute('''
                UPDATE client_accounts
                SET password_hash = ?, last_login = ?
                WHERE id = ?
            ''', (password, datetime.now(), account['id']))
            conn.commit()
            conn.close()

            session['client_account_id'] = account['id']
            session['client_id']          = account['client_id']
            session['client_name']        = account['client_name']
            session['account_type']       = 'client'
            session['client_theme'] = 'light'
            return redirect(url_for('client_portal'))

        return render_template('client/set_password.html')


    @app.route('/client-logout')
    def client_logout():
        session.pop('client_account_id', None)
        session.pop('client_id', None)
        session.pop('client_name', None)
        session.pop('account_type', None)
        session.pop('client_theme', None)
        return redirect(url_for('login'))


    # ── Client portal pages ────────────────────────────────────────────────────

    @app.route('/client-portal')
    @client_login_required
    def client_portal():
        """Main client dashboard."""
        client_id = session['client_id']
        conn = get_db()

        client = conn.execute(
            'SELECT * FROM clients WHERE id = ?', (client_id,)
        ).fetchone()

        # Latest weight
        latest_weight = conn.execute('''
            SELECT weight, date FROM weight_logs
            WHERE client_id = ? ORDER BY date DESC LIMIT 1
        ''', (client_id,)).fetchone()

        # Recent workouts
        recent_workouts = conn.execute('''
            SELECT workout_date, COUNT(*) as exercise_count
            FROM workout_logs WHERE client_id = ?
            GROUP BY workout_date ORDER BY workout_date DESC LIMIT 5
        ''', (client_id,)).fetchall()

        # Upcoming sessions
        upcoming_sessions = conn.execute('''
            SELECT session_date, start_time, end_time, session_type, status
            FROM sessions
            WHERE client_id = ? AND session_date >= date('now') AND status != 'cancelled'
            ORDER BY session_date, start_time LIMIT 3
        ''', (client_id,)).fetchall()

        # Latest sleep
        latest_sleep = conn.execute('''
            SELECT hours, date FROM sleep_logs
            WHERE client_id = ? ORDER BY date DESC LIMIT 1
        ''', (client_id,)).fetchone()

        conn.close()

        return render_template('client/index.html',
                               client=client,
                               latest_weight=latest_weight,
                               recent_workouts=recent_workouts,
                               upcoming_sessions=upcoming_sessions,
                               latest_sleep=latest_sleep)

    @app.route('/client-portal/workouts')
    @client_login_required
    def client_portal_workouts():
        client_id = session['client_id']
        conn = get_db()
        client = conn.execute('SELECT * FROM clients WHERE id = ?', (client_id,)).fetchone()
        workouts = conn.execute('''
            SELECT workout_date, workout_type, COUNT(*) as exercise_count
            FROM workout_logs WHERE client_id = ?
            GROUP BY workout_date, workout_type ORDER BY workout_date DESC, workout_type
        ''', (client_id,)).fetchall()
        acct = conn.execute(
            'SELECT perm_workouts FROM client_accounts WHERE client_id = ?', (client_id,)
        ).fetchone()
        can_edit = bool(acct and acct['perm_workouts'])
        conn.close()
        return render_template('client/workouts.html', client=client, workouts=workouts, can_edit=can_edit)

    @app.route('/client-portal/workouts/<date>')
    @client_login_required
    def client_portal_workout_detail(date):
        client_id = session['client_id']
        conn = get_db()
        # Optional ?type=weightlifting|cardio — omitted means "all types for
        # this date" (kept for backward compatibility with any older caller).
        workout_type = request.args.get('type')
        if workout_type in ('weightlifting', 'cardio'):
            exercises = conn.execute('''
                SELECT id, exercise_name, sets_data, notes, tags, workout_type
                FROM workout_logs
                WHERE client_id = ? AND workout_date = ? AND workout_type = ?
                ORDER BY created_at
            ''', (client_id, date, workout_type)).fetchall()
        else:
            exercises = conn.execute('''
                SELECT id, exercise_name, sets_data, notes, tags, workout_type
                FROM workout_logs
                WHERE client_id = ? AND workout_date = ?
                ORDER BY created_at
            ''', (client_id, date)).fetchall()
        conn.close()

        result = []
        for ex in exercises:
            sets_data = json.loads(ex['sets_data']) if ex['sets_data'] else None
            result.append({
                'id': ex['id'],
                'exercise_name': ex['exercise_name'],
                'notes': ex['notes'],
                'sets_data': sets_data,
                'tags': ex['tags'] or '',
                'workout_type': ex['workout_type'] if 'workout_type' in ex.keys() else 'weightlifting'
            })
        return jsonify(result)

    @app.route('/client-portal/api/workouts', methods=['POST'])
    @client_login_required
    def client_log_workout():
        client_id = session['client_id']
        conn = get_db()
        try:
            acct = conn.execute('SELECT perm_workouts FROM client_accounts WHERE client_id = ?', (client_id,)).fetchone()
            if not acct or not acct['perm_workouts']:
                conn.close()
                return jsonify({'error': 'Permission denied'}), 403
            client_row = conn.execute('SELECT trainer_id FROM clients WHERE id = ?', (client_id,)).fetchone()
            trainer_id = client_row['trainer_id'] if client_row else None
            data = request.get_json()
            workout_date = data.get('date')
            exercises    = data.get('exercises', [])
            workout_tags = data.get('tags', '')
            override     = data.get('override', False)
            workout_type = data.get('workout_type', 'weightlifting')
            if workout_type not in ('weightlifting', 'cardio'):
                workout_type = 'weightlifting'
            if not workout_date or not exercises:
                conn.close()
                return jsonify({'error': 'Missing date or exercises'}), 400

            # Cardio workouts always carry a "Cardio" tag that can't be
            # removed from the form — enforced here regardless of what was
            # submitted, so the only way to get rid of it is deleting the
            # whole workout.
            if workout_type == 'cardio':
                tag_list = [t.strip() for t in workout_tags.split(',') if t.strip()]
                if 'Cardio' not in tag_list:
                    tag_list.append('Cardio')
                workout_tags = ','.join(tag_list)

            if not override:
                existing = conn.execute(
                    'SELECT 1 FROM workout_logs WHERE client_id = ? AND workout_date = ? AND workout_type = ? LIMIT 1',
                    (client_id, workout_date, workout_type)
                ).fetchone()
                if existing:
                    conn.close()
                    return jsonify({'conflict': True}), 409
            for ex in exercises:
                if workout_type == 'cardio':
                    sets = ex.get('sets', []) or [{'distance': None, 'distance_unit': None, 'duration': None, 'duration_unit': None, 'notes': None}]
                    total_sets = len(sets)
                    conn.execute('''
                        INSERT INTO workout_logs
                        (id, client_id, trainer_id, exercise_name, sets, reps, weight, notes, workout_date, sets_data, tags, workout_type, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (str(uuid.uuid4()), client_id, trainer_id, ex['name'], total_sets, None, None,
                          ex.get('notes', ''), workout_date, json.dumps(sets), workout_tags, workout_type, datetime.now()))
                else:
                    sets = ex.get('sets', []) or [{'weight': None, 'reps': None}]
                    total_sets = len(sets)
                    avg_weight = (sum(s['weight'] for s in sets if s.get('weight')) / len([s for s in sets if s.get('weight')])) if any(s.get('weight') for s in sets) else None
                    avg_reps   = (sum(s['reps'] for s in sets if s.get('reps')) // len([s for s in sets if s.get('reps')])) if any(s.get('reps') for s in sets) else None
                    conn.execute('''
                        INSERT INTO workout_logs
                        (id, client_id, trainer_id, exercise_name, sets, reps, weight, notes, workout_date, sets_data, tags, workout_type, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (str(uuid.uuid4()), client_id, trainer_id, ex['name'], total_sets, avg_reps, avg_weight,
                          ex.get('notes', ''), workout_date, json.dumps(sets), workout_tags, workout_type, datetime.now()))
            log_activity(conn, client_id, 'workout', 'created', workout_date)
            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except Exception as e:
            conn.close()
            return jsonify({'error': str(e)}), 500


    @app.route('/client-portal/api/workouts/<date>', methods=['PUT'])
    @client_login_required
    def client_edit_workout(date):
        client_id = session['client_id']
        conn = get_db()
        try:
            acct = conn.execute('SELECT perm_workouts FROM client_accounts WHERE client_id = ?', (client_id,)).fetchone()
            if not acct or not acct['perm_workouts']:
                conn.close()
                return jsonify({'error': 'Permission denied'}), 403
            client_row = conn.execute('SELECT trainer_id FROM clients WHERE id = ?', (client_id,)).fetchone()
            trainer_id = client_row['trainer_id'] if client_row else None
            data = request.get_json()
            new_date  = data.get('date', date)
            exercises = data.get('exercises', [])
            workout_tags = data.get('tags', '')
            workout_type = data.get('workout_type', 'weightlifting')
            if workout_type not in ('weightlifting', 'cardio'):
                workout_type = 'weightlifting'

            if workout_type == 'cardio':
                tag_list = [t.strip() for t in workout_tags.split(',') if t.strip()]
                if 'Cardio' not in tag_list:
                    tag_list.append('Cardio')
                workout_tags = ','.join(tag_list)

            # Delete only this workout's own (date, type) pair — a
            # weightlifting and a cardio workout on the same date are
            # independent and must not affect each other when one is edited.
            conn.execute('DELETE FROM workout_logs WHERE client_id = ? AND workout_date = ? AND workout_type = ?', (client_id, date, workout_type))
            for ex in exercises:
                if workout_type == 'cardio':
                    sets = ex.get('sets', []) or [{'distance': None, 'distance_unit': None, 'duration': None, 'duration_unit': None, 'notes': None}]
                    total_sets = len(sets)
                    conn.execute('''
                        INSERT INTO workout_logs
                        (id, client_id, trainer_id, exercise_name, sets, reps, weight, notes, workout_date, sets_data, tags, workout_type, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (str(uuid.uuid4()), client_id, trainer_id, ex['name'], total_sets, None, None,
                          ex.get('notes', ''), new_date, json.dumps(sets), workout_tags, workout_type, datetime.now()))
                else:
                    sets = ex.get('sets', []) or [{'weight': None, 'reps': None}]
                    total_sets = len(sets)
                    avg_weight = (sum(s['weight'] for s in sets if s.get('weight')) / len([s for s in sets if s.get('weight')])) if any(s.get('weight') for s in sets) else None
                    avg_reps   = (sum(s['reps'] for s in sets if s.get('reps')) // len([s for s in sets if s.get('reps')])) if any(s.get('reps') for s in sets) else None
                    conn.execute('''
                        INSERT INTO workout_logs
                        (id, client_id, trainer_id, exercise_name, sets, reps, weight, notes, workout_date, sets_data, tags, workout_type, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (str(uuid.uuid4()), client_id, trainer_id, ex['name'], total_sets, avg_reps, avg_weight,
                          ex.get('notes', ''), new_date, json.dumps(sets), workout_tags, workout_type, datetime.now()))
            log_activity(conn, client_id, 'workout', 'updated', new_date)
            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except Exception as e:
            conn.close()
            return jsonify({'error': str(e)}), 500


    @app.route('/client-portal/api/workouts/<date>', methods=['DELETE'])
    @client_login_required
    def client_delete_workout(date):
        client_id = session['client_id']
        conn = get_db()
        try:
            acct = conn.execute('SELECT perm_workouts FROM client_accounts WHERE client_id = ?', (client_id,)).fetchone()
            if not acct or not acct['perm_workouts']:
                conn.close()
                return jsonify({'error': 'Permission denied'}), 403
            # workout_type is required so deleting one workout (e.g. cardio)
            # on a date can never also wipe out a different workout type
            # logged the same day. Falls back to 'weightlifting' only for
            # any pre-existing caller that predates this parameter.
            workout_type = request.args.get('type', 'weightlifting')
            if workout_type not in ('weightlifting', 'cardio'):
                workout_type = 'weightlifting'
            conn.execute('DELETE FROM workout_logs WHERE client_id = ? AND workout_date = ? AND workout_type = ?', (client_id, date, workout_type))
            log_activity(conn, client_id, 'workout', 'deleted', date)
            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except Exception as e:
            conn.close()
            return jsonify({'error': str(e)}), 500


    @app.route('/client-portal/api/workouts/duplicate', methods=['POST'])
    @client_login_required
    def client_duplicate_workout():
        """Client duplicates an existing workout to a new date."""
        client_id = session['client_id']
        conn = get_db()
        try:
            acct = conn.execute('SELECT perm_workouts FROM client_accounts WHERE client_id = ?', (client_id,)).fetchone()
            if not acct or not acct['perm_workouts']:
                conn.close()
                return jsonify({'error': 'Permission denied'}), 403

            # trainer_id is required by the DB constraint — pull it from the client record
            client_row = conn.execute('SELECT trainer_id FROM clients WHERE id = ?', (client_id,)).fetchone()
            trainer_id = client_row['trainer_id'] if client_row else None

            data = request.get_json()
            original_date = data.get('original_date')
            new_date = data.get('new_date')
            override = data.get('override', False)
            workout_type = data.get('workout_type', 'weightlifting')
            if workout_type not in ('weightlifting', 'cardio'):
                workout_type = 'weightlifting'
            if not original_date or not new_date:
                conn.close()
                return jsonify({'error': 'Missing date parameters'}), 400

            if not override:
                existing = conn.execute(
                    'SELECT 1 FROM workout_logs WHERE client_id = ? AND workout_date = ? AND workout_type = ? LIMIT 1',
                    (client_id, new_date, workout_type)
                ).fetchone()
                if existing:
                    conn.close()
                    return jsonify({'conflict': True}), 409

            # Same type only — a cardio "duplicate" button only ever
            # duplicates the cardio entry for that date, never an unrelated
            # weightlifting entry logged the same day.
            exercises = conn.execute('''
                SELECT exercise_name, sets_data, notes, tags
                FROM workout_logs
                WHERE client_id = ? AND workout_date = ? AND workout_type = ?
                ORDER BY created_at
            ''', (client_id, original_date, workout_type)).fetchall()

            if not exercises:
                conn.close()
                return jsonify({'error': 'No workout found for that date'}), 404

            default_sets = ([{'distance': None, 'distance_unit': None, 'duration': None, 'duration_unit': None, 'notes': None}]
                             if workout_type == 'cardio' else [{'weight': None, 'reps': None}])
            for exercise in exercises:
                sets_data = exercise['sets_data']
                exercise_sets = json.loads(sets_data) if sets_data else default_sets
                total_sets = len(exercise_sets)
                if workout_type == 'cardio':
                    avg_reps = None
                    avg_weight = None
                else:
                    avg_reps = sum(s['reps'] for s in exercise_sets if s.get('reps')) // len(
                        [s for s in exercise_sets if s.get('reps')]) if any(s.get('reps') for s in exercise_sets) else None
                    avg_weight = sum(s['weight'] for s in exercise_sets if s.get('weight')) / len(
                        [s for s in exercise_sets if s.get('weight')]) if any(s.get('weight') for s in exercise_sets) else None
                conn.execute('''
                    INSERT INTO workout_logs (id, client_id, trainer_id, exercise_name, sets, reps, weight, notes, workout_date, sets_data, tags, workout_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (str(uuid.uuid4()), client_id, trainer_id, exercise['exercise_name'],
                      total_sets, avg_reps, avg_weight, exercise['notes'], new_date, sets_data, exercise['tags'],
                      workout_type, datetime.now()))

            log_activity(conn, client_id, 'workout', 'duplicated', new_date)
            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except Exception as e:
            conn.close()
            return jsonify({'error': str(e)}), 500


    @app.route('/client-portal/weight', methods=['GET', 'POST'])
    @client_login_required
    def client_portal_weight():
        """Client logs and views their weight."""
        client_id = session['client_id']
        conn = get_db()

        if request.method == 'POST':
            data = request.get_json()
            override = data.get('override', False)

            existing = conn.execute(
                'SELECT id, weight, notes FROM weight_logs WHERE client_id = ? AND date = ?',
                (client_id, data['date'])
            ).fetchone()

            if existing and not override:
                conn.close()
                return jsonify({
                    'conflict': True,
                    'existing': {'id': existing['id'], 'weight': existing['weight'], 'notes': existing['notes']}
                }), 409

            if existing and override:
                # Replace the existing entry for that date
                conn.execute(
                    'UPDATE weight_logs SET weight = ?, notes = ? WHERE id = ? AND client_id = ?',
                    (data['weight'], data.get('notes', ''), existing['id'], client_id)
                )
                entry_id = existing['id']
                log_activity(conn, client_id, 'weight', 'updated', data['date'])
            else:
                entry_id = str(uuid.uuid4())
                conn.execute('''
                    INSERT INTO weight_logs (id, client_id, date, weight, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (entry_id, client_id, data['date'], data['weight'],
                      data.get('notes', ''), datetime.now()))
                log_activity(conn, client_id, 'weight', 'created', data['date'])

            conn.commit()
            conn.close()
            return jsonify({'success': True, 'id': entry_id})

        weight_history = conn.execute('''
            SELECT id, date, weight, notes FROM weight_logs
            WHERE client_id = ? ORDER BY date DESC
        ''', (client_id,)).fetchall()

        client = conn.execute('SELECT * FROM clients WHERE id = ?', (client_id,)).fetchone()
        acct = conn.execute('SELECT perm_weight FROM client_accounts WHERE client_id = ?', (client_id,)).fetchone()
        can_edit = bool(acct and acct['perm_weight'])
        conn.close()

        return render_template('client/weight.html',
                               client=client,
                               weight_history=[dict(r) for r in weight_history],
                               can_edit=can_edit)

    @app.route('/client-portal/api/weight/<entry_id>', methods=['PUT'])
    @client_login_required
    def client_update_weight(entry_id):
        client_id = session['client_id']
        data = request.get_json()
        conn = get_db()
        conn.execute(
            'UPDATE weight_logs SET date = ?, weight = ?, notes = ? WHERE id = ? AND client_id = ?',
            (data['date'], data['weight'], data.get('notes', ''), entry_id, client_id)
        )
        log_activity(conn, client_id, 'weight', 'updated', data['date'])
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/client-portal/sleep', methods=['GET', 'POST'])
    @client_login_required
    def client_portal_sleep():
        """Client logs and views their sleep."""
        client_id = session['client_id']
        conn = get_db()

        if request.method == 'POST':
            data = request.get_json()
            override = data.get('override', False)

            existing = conn.execute(
                'SELECT id, hours, notes FROM sleep_logs WHERE client_id = ? AND date = ?',
                (client_id, data['date'])
            ).fetchone()

            if existing and not override:
                conn.close()
                return jsonify({
                    'conflict': True,
                    'existing': {'id': existing['id'], 'hours': existing['hours'], 'notes': existing['notes']}
                }), 409

            if existing and override:
                # Replace the existing entry for that date
                conn.execute(
                    'UPDATE sleep_logs SET hours = ?, notes = ? WHERE id = ? AND client_id = ?',
                    (data['hours'], data.get('notes', ''), existing['id'], client_id)
                )
                entry_id = existing['id']
                log_activity(conn, client_id, 'sleep', 'updated', data['date'])
            else:
                entry_id = str(uuid.uuid4())
                conn.execute('''
                    INSERT INTO sleep_logs (id, client_id, date, hours, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (entry_id, client_id, data['date'], data['hours'],
                      data.get('notes', ''), datetime.now()))
                log_activity(conn, client_id, 'sleep', 'created', data['date'])

            conn.commit()
            conn.close()
            return jsonify({'success': True, 'id': entry_id})

        sleep_history = conn.execute('''
            SELECT id, date, hours, notes FROM sleep_logs
            WHERE client_id = ? ORDER BY date DESC
        ''', (client_id,)).fetchall()

        client = conn.execute('SELECT * FROM clients WHERE id = ?', (client_id,)).fetchone()
        acct = conn.execute('SELECT perm_sleep FROM client_accounts WHERE client_id = ?', (client_id,)).fetchone()
        can_edit = bool(acct and acct['perm_sleep'])
        conn.close()

        return render_template('client/sleep.html',
                               client=client,
                               sleep_history=[dict(r) for r in sleep_history],
                               can_edit=can_edit)


    @app.route('/client-portal/nutrition', methods=['GET', 'POST'])
    @client_login_required
    def client_portal_nutrition():
        """Client logs and views their nutrition."""
        client_id = session['client_id']
        conn = get_db()

        if request.method == 'POST':
            data = request.get_json()
            override = data.get('override', False)

            existing = conn.execute(
                'SELECT id, diet, estimated_calories, estimated_protein, estimated_sodium, estimated_saturated_fat, notes '
                'FROM nutrition_logs WHERE client_id = ? AND date = ?',
                (client_id, data['date'])
            ).fetchone()

            if existing and not override:
                conn.close()
                return jsonify({
                    'conflict': True,
                    'existing': {
                        'id': existing['id'],
                        'diet': existing['diet'],
                        'estimated_calories': existing['estimated_calories'],
                        'estimated_protein': existing['estimated_protein'],
                        'estimated_sodium': existing['estimated_sodium'],
                        'estimated_saturated_fat': existing['estimated_saturated_fat'],
                        'notes': existing['notes']
                    }
                }), 409

            if existing and override:
                conn.execute('''
                    UPDATE nutrition_logs
                    SET diet = ?, estimated_calories = ?, estimated_protein = ?,
                        estimated_sodium = ?, estimated_saturated_fat = ?, notes = ?
                    WHERE id = ? AND client_id = ?
                ''', (data.get('diet', ''), data.get('estimated_calories'), data.get('estimated_protein'),
                      data.get('estimated_sodium'), data.get('estimated_saturated_fat'),
                      data.get('notes', ''), existing['id'], client_id))
                entry_id = existing['id']
                log_activity(conn, client_id, 'nutrition', 'updated', data['date'])
            else:
                entry_id = str(uuid.uuid4())
                conn.execute('''
                    INSERT INTO nutrition_logs
                    (id, client_id, date, diet, estimated_calories, estimated_protein,
                     estimated_sodium, estimated_saturated_fat, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (entry_id, client_id, data['date'], data.get('diet', ''),
                      data.get('estimated_calories'), data.get('estimated_protein'),
                      data.get('estimated_sodium'), data.get('estimated_saturated_fat'),
                      data.get('notes', ''), datetime.now()))
                log_activity(conn, client_id, 'nutrition', 'created', data['date'])

            conn.commit()
            conn.close()
            return jsonify({'success': True, 'id': entry_id})

        nutrition_history = conn.execute('''
            SELECT id, date, diet, estimated_calories, estimated_protein,
                   estimated_sodium, estimated_saturated_fat, notes
            FROM nutrition_logs
            WHERE client_id = ? ORDER BY date DESC
        ''', (client_id,)).fetchall()

        client = conn.execute('SELECT * FROM clients WHERE id = ?', (client_id,)).fetchone()
        acct = conn.execute('SELECT perm_nutrition FROM client_accounts WHERE client_id = ?', (client_id,)).fetchone()
        can_edit = bool(acct and acct['perm_nutrition'])
        conn.close()

        return render_template('client/nutrition.html',
                               client=client,
                               nutrition_history=[dict(r) for r in nutrition_history],
                               can_edit=can_edit)


    # ── Client portal API — delete own entries ─────────────────────────────────

    @app.route('/client-portal/api/weight/<entry_id>', methods=['DELETE'])
    @client_login_required
    def client_delete_weight(entry_id):
        client_id = session['client_id']
        conn = get_db()
        conn.execute(
            'DELETE FROM weight_logs WHERE id = ? AND client_id = ?',
            (entry_id, client_id)
        )
        log_activity(conn, client_id, 'weight', 'deleted', '')
        conn.commit()
        conn.close()
        return jsonify({'success': True})


    @app.route('/client-portal/api/sleep/<entry_id>', methods=['DELETE'])
    @client_login_required
    def client_delete_sleep(entry_id):
        client_id = session['client_id']
        conn = get_db()
        conn.execute(
            'DELETE FROM sleep_logs WHERE id = ? AND client_id = ?',
            (entry_id, client_id)
        )
        log_activity(conn, client_id, 'sleep', 'deleted', '')
        conn.commit()
        conn.close()
        return jsonify({'success': True})


    @app.route('/client-portal/api/sleep/<entry_id>', methods=['PUT'])
    @client_login_required
    def client_update_sleep(entry_id):
        client_id = session['client_id']
        data = request.get_json()
        if not data or 'date' not in data or 'hours' not in data:
            return jsonify({'error': 'Missing date or hours'}), 400
        conn = get_db()
        entry = conn.execute(
            'SELECT id FROM sleep_logs WHERE id = ? AND client_id = ?',
            (entry_id, client_id)
        ).fetchone()
        if not entry:
            conn.close()
            return jsonify({'error': 'Entry not found'}), 404
        conn.execute(
            'UPDATE sleep_logs SET date = ?, hours = ?, notes = ? WHERE id = ? AND client_id = ?',
            (data['date'], data['hours'], data.get('notes', ''), entry_id, client_id)
        )
        log_activity(conn, client_id, 'sleep', 'updated', data['date'])
        conn.commit()
        conn.close()
        return jsonify({'success': True})


    @app.route('/client-portal/api/nutrition/<entry_id>', methods=['PUT'])
    @client_login_required
    def client_update_nutrition(entry_id):
        client_id = session['client_id']
        data = request.get_json()
        if not data or 'date' not in data:
            return jsonify({'error': 'Missing date'}), 400
        conn = get_db()
        entry = conn.execute(
            'SELECT id FROM nutrition_logs WHERE id = ? AND client_id = ?',
            (entry_id, client_id)
        ).fetchone()
        if not entry:
            conn.close()
            return jsonify({'error': 'Entry not found'}), 404
        conn.execute('''
            UPDATE nutrition_logs
            SET date = ?, diet = ?, estimated_calories = ?, estimated_protein = ?,
                estimated_sodium = ?, estimated_saturated_fat = ?, notes = ?
            WHERE id = ? AND client_id = ?
        ''', (
            data['date'],
            data.get('diet', ''),
            data.get('estimated_calories'),
            data.get('estimated_protein'),
            data.get('estimated_sodium'),
            data.get('estimated_saturated_fat'),
            data.get('notes', ''),
            entry_id,
            client_id
        ))
        log_activity(conn, client_id, 'nutrition', 'updated', data['date'])
        conn.commit()
        conn.close()
        return jsonify({'success': True})