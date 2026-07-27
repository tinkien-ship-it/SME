"""Routes bảng chấm công — nhập file & nhận dữ liệu máy ZKTeco (ADMS)."""
import csv
import io

from flask import Response, jsonify, render_template, request

from auth import login_required
from db_utils import get_db_connection
from Services.attendance_helpers import (
    build_daily_summary,
    ensure_attendance_schema,
    parse_zkteco_attlog_line,
    touch_device,
    upsert_attendance_log,
)


def register_attendance_routes(app):

    @app.route('/attendance')
    @login_required
    def attendance_page():
        return render_template('attendance.html')

    @app.route('/api/attendance/logs', methods=['GET'])
    @login_required
    def api_attendance_logs():
        start = (request.args.get('start') or '').strip()
        end = (request.args.get('end') or '').strip()
        q = (request.args.get('q') or '').strip()
        employee_id = request.args.get('employee_id', type=int)

        conn = get_db_connection()
        try:
            ensure_attendance_schema(conn)
            sql = """
                SELECT l.*, e.fullname AS mapped_name
                FROM attendance_logs l
                LEFT JOIN employees e ON e.id = l.employee_id
                WHERE 1=1
            """
            params = []
            if start:
                sql += ' AND l.punch_date >= ?'
                params.append(start)
            if end:
                sql += ' AND l.punch_date <= ?'
                params.append(end)
            if employee_id:
                sql += ' AND l.employee_id = ?'
                params.append(employee_id)
            if q:
                like = f'%{q}%'
                sql += """
                    AND (
                        COALESCE(l.device_user_id, '') LIKE ?
                        OR COALESCE(l.employee_name, '') LIKE ?
                        OR COALESCE(e.fullname, '') LIKE ?
                        OR COALESCE(l.device_sn, '') LIKE ?
                    )
                """
                params.extend([like] * 4)
            sql += ' ORDER BY l.punch_time DESC, l.id DESC LIMIT 5000'
            rows = conn.execute(sql, params).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:
            conn.close()

    @app.route('/api/attendance/summary', methods=['GET'])
    @login_required
    def api_attendance_summary():
        start = (request.args.get('start') or '').strip()
        end = (request.args.get('end') or '').strip()
        if not start or not end:
            return jsonify({'error': 'Thiếu khoảng ngày'}), 400
        employee_id = request.args.get('employee_id', type=int)
        conn = get_db_connection()
        try:
            data = build_daily_summary(conn, start, end, employee_id)
            return jsonify(data)
        finally:
            conn.close()

    @app.route('/api/attendance/devices', methods=['GET'])
    @login_required
    def api_attendance_devices():
        conn = get_db_connection()
        try:
            ensure_attendance_schema(conn)
            rows = conn.execute(
                'SELECT * FROM attendance_devices ORDER BY last_seen_at DESC, id DESC'
            ).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:
            conn.close()

    @app.route('/api/attendance/import', methods=['POST'])
    @login_required
    def api_attendance_import():
        payload = request.get_json(silent=True) or {}
        records = payload.get('records') or []
        device_sn = (payload.get('device_sn') or 'IMPORT').strip()
        if not records:
            return jsonify({'error': 'Không có dữ liệu'}), 400

        conn = get_db_connection()
        ok = 0
        skipped = 0
        errors = []
        try:
            ensure_attendance_schema(conn)
            for idx, rec in enumerate(records, start=1):
                success, err = upsert_attendance_log(conn, rec, source='import', device_sn=device_sn)
                if success:
                    ok += 1
                elif err == 'invalid_time':
                    skipped += 1
                else:
                    errors.append(f'Dòng {idx}: {err}')
            conn.commit()
            return jsonify({
                'success': True,
                'imported': ok,
                'skipped': skipped,
                'errors': errors[:20],
            })
        except Exception as e:
            conn.rollback()
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/attendance/import-csv', methods=['POST'])
    @login_required
    def api_attendance_import_csv():
        upload = request.files.get('file')
        if not upload:
            return jsonify({'error': 'Thiếu file'}), 400
        raw = upload.read()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('latin-1', errors='replace')

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return jsonify({'error': 'File CSV không có header'}), 400

        def pick(row, *keys):
            lower = {str(k).strip().lower(): v for k, v in row.items()}
            for key in keys:
                for col, val in lower.items():
                    if key in col:
                        return str(val or '').strip()
            return ''

        records = []
        for row in reader:
            pin = pick(row, 'pin', 'mã', 'ma', 'userid', 'user id', 'id nv', 'mã nv')
            name = pick(row, 'name', 'tên', 'ten', 'họ tên', 'ho ten', 'fullname')
            dt_text = pick(row, 'datetime', 'date time', 'ngày giờ', 'ngay gio')
            if not dt_text:
                date_part = pick(row, 'date', 'ngày', 'ngay')
                time_part = pick(row, 'time', 'giờ', 'gio')
                if date_part and time_part:
                    dt_text = f'{date_part} {time_part}'
            status = pick(row, 'status', 'trạng thái', 'trang thai', 'type', 'in/out')
            if not pin and not dt_text:
                continue
            records.append({
                'device_user_id': pin,
                'employee_name': name,
                'datetime': dt_text,
                'punch_type': status,
            })

        conn = get_db_connection()
        ok = 0
        try:
            ensure_attendance_schema(conn)
            for rec in records:
                success, _ = upsert_attendance_log(conn, rec, source='import_csv', device_sn='CSV')
                if success:
                    ok += 1
            conn.commit()
            return jsonify({'success': True, 'imported': ok, 'total_rows': len(records)})
        finally:
            conn.close()

    @app.route('/api/attendance/manual', methods=['POST'])
    @login_required
    def api_attendance_manual():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            success, err = upsert_attendance_log(conn, data, source='manual', device_sn='MANUAL')
            if not success:
                return jsonify({'error': err or 'Không lưu được'}), 400
            conn.commit()
            return jsonify({'success': True})
        finally:
            conn.close()

    @app.route('/api/attendance/map-employee', methods=['POST'])
    @login_required
    def api_attendance_map_employee():
        data = request.get_json(silent=True) or {}
        employee_id = data.get('employee_id')
        attendance_code = (data.get('attendance_code') or '').strip()
        if not employee_id or not attendance_code:
            return jsonify({'error': 'Thiếu employee_id hoặc mã máy chấm công'}), 400
        conn = get_db_connection()
        try:
            conn.execute(
                'UPDATE employees SET attendance_code = ? WHERE id = ?',
                (attendance_code, employee_id),
            )
            conn.execute(
                """
                UPDATE attendance_logs
                SET employee_id = ?
                WHERE device_user_id = ? AND (employee_id IS NULL OR employee_id = 0)
                """,
                (employee_id, attendance_code),
            )
            conn.commit()
            return jsonify({'success': True})
        finally:
            conn.close()

    # --- ZKTeco ADMS / iClock (máy chấm công push dữ liệu) ---
    @app.route('/iclock/cdata', methods=['GET', 'POST'])
    def iclock_cdata():
        serial = (request.args.get('SN') or request.args.get('sn') or 'UNKNOWN').strip()
        table = (request.args.get('table') or '').strip().upper()
        ip = request.remote_addr

        conn = get_db_connection()
        try:
            ensure_attendance_schema(conn)
            touch_device(conn, serial, ip)

            if request.method == 'GET':
                conn.commit()
                return Response(
                    f"GET OPTION FROM: {serial}\n"
                    "Stamp=9999\nOpStamp=9999\nErrorDelay=60\nDelay=30\n"
                    "TransTimes=00:00;23:59\nTransInterval=1\n"
                    "TransFlag=TransData AttLog OpLog\nRealtime=1\nEncrypt=0\n",
                    mimetype='text/plain',
                )

            body = request.get_data(as_text=True) or ''
            if table == 'ATTLOG' or '\t' in body:
                for line in body.splitlines():
                    parsed = parse_zkteco_attlog_line(line)
                    if not parsed:
                        continue
                    upsert_attendance_log(conn, parsed, source='adms', device_sn=serial)
            conn.commit()
            return Response('OK', mimetype='text/plain')
        except Exception:
            conn.rollback()
            return Response('OK', mimetype='text/plain')
        finally:
            conn.close()

    @app.route('/iclock/getrequest', methods=['GET'])
    def iclock_getrequest():
        serial = (request.args.get('SN') or 'UNKNOWN').strip()
        conn = get_db_connection()
        try:
            touch_device(conn, serial, request.remote_addr)
            conn.commit()
        finally:
            conn.close()
        return Response('OK', mimetype='text/plain')

    @app.route('/iclock/devicecmd', methods=['POST'])
    def iclock_devicecmd():
        return Response('OK', mimetype='text/plain')
