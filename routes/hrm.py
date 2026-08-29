# -*- coding: utf-8 -*-
"""Routes HRM modular — contracts, shifts, OT, formulas, ESS, compliance, export."""
from __future__ import annotations

import re

from flask import Response, flash, jsonify, redirect, render_template, request, session, url_for

from auth import ess_portal_required, login_required
from db.dialect import is_locked_error
from db_utils import get_db_connection, locked_user_message, sqlite_commit


def _conn():
    return get_db_connection()


def _api_error(exc: Exception, *, status: int = 400):
    if is_locked_error(exc):
        return jsonify({'success': False, 'error': locked_user_message()}), 503
    return jsonify({'success': False, 'error': str(exc)}), status


def _actor():
    return (
        session.get('user_name')
        or session.get('username')
        or (session.get('user') or {}).get('username')
        or ''
    )


def _can_manage_ess_link() -> bool:
    from Services.hrm.ess_access import session_may_manage_ess_link
    return session_may_manage_ess_link()


def register_hrm_routes(app):
    try:
        from Services.tenant_profile import require_sme_regime
    except Exception:
        def require_sme_regime(f):
            return f

    # ── Pages ──────────────────────────────────────────────────────────

    @app.route('/hrm/contracts')
    @app.route('/SME_hrm_contracts')
    @login_required
    @require_sme_regime
    def SME_hrm_contracts():
        return render_template('hrm/contracts.html')

    @app.route('/hrm/contracts/<int:contract_id>/print')
    @app.route('/SME_hrm_contracts/<int:contract_id>/print')
    @login_required
    def SME_hrm_contract_print(contract_id):
        from Services.hrm.contract_templates import build_contract_print_context
        from Services.hrm import contract_template_store as ld_tpl
        conn = _conn()
        try:
            try:
                ctx = build_contract_print_context(conn, contract_id)
            except ValueError:
                flash('Không tìm thấy hợp đồng', 'warning')
                return redirect(url_for('SME_hrm_contracts'))
            html_out = ld_tpl.render_contract_html(conn, ctx, app)
            resp = app.make_response(html_out)
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        finally:
            conn.close()

    @app.route('/api/hrm/contracts/template', methods=['GET', 'PUT', 'DELETE'])
    @login_required
    def api_hrm_contract_template():
        from Services.hrm import contract_template_store as ld_tpl
        from Services.hrm.contracts import CONTRACT_TYPES
        conn = _conn()
        try:
            ctype = (request.args.get('type') or 'indefinite').strip()
            if ctype not in CONTRACT_TYPES:
                return jsonify({'error': 'Loại HĐ không hợp lệ'}), 400
            if request.method == 'GET':
                meta = ld_tpl.template_meta(conn, ctype)
                custom = ld_tpl.get_custom_template_html(conn, ctype)
                body = custom or ld_tpl.render_system_default_template(app, ctype)
                return jsonify({
                    'contract_type': ctype,
                    'type_label': CONTRACT_TYPES.get(ctype),
                    'html': body,
                    'is_custom': meta['is_custom'],
                    'tenant_scoped': True,
                    'scope_note': 'Mẫu chỉ lưu trong dữ liệu doanh nghiệp hiện tại; tenant khác không bị ảnh hưởng.',
                    'placeholders': ld_tpl.placeholders_guide(),
                    'used': ld_tpl.extract_placeholders(body),
                })
            if request.method == 'DELETE':
                ld_tpl.reset_custom_template(conn, ctype)
                sqlite_commit(conn, label='hrm_ld_tpl_reset')
                return jsonify({
                    'success': True,
                    'html': ld_tpl.render_system_default_template(app, ctype),
                    'message': 'Đã khôi phục mẫu mặc định hệ thống cho loại HĐ này.',
                })
            data = request.get_json(silent=True) or {}
            html_body = data.get('html')
            if html_body is None and request.data:
                html_body = request.get_data(as_text=True)
            ld_tpl.set_custom_template_html(conn, ctype, html_body or '')
            sqlite_commit(conn, label='hrm_ld_tpl')
            return jsonify({'success': True})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/hrm/contracts/template/export')
    @login_required
    def api_hrm_contract_template_export():
        from flask import Response
        from Services.hrm import contract_template_store as ld_tpl
        from Services.hrm.contracts import CONTRACT_TYPES
        conn = _conn()
        try:
            ctype = (request.args.get('type') or 'indefinite').strip()
            if ctype not in CONTRACT_TYPES:
                return jsonify({'error': 'Loại HĐ không hợp lệ'}), 400
            custom = ld_tpl.get_custom_template_html(conn, ctype)
            body = custom or ld_tpl.render_system_default_template(app, ctype)
            fname = f'mau-hdld-{ctype}.html'
            return Response(
                body,
                mimetype='text/html; charset=utf-8',
                headers={'Content-Disposition': f'attachment; filename="{fname}"'},
            )
        finally:
            conn.close()

    @app.route('/api/hrm/contracts/template/import', methods=['POST'])
    @login_required
    def api_hrm_contract_template_import():
        import html as html_mod
        import io
        import zipfile

        from Services.hrm import contract_template_store as ld_tpl
        from Services.hrm.contracts import CONTRACT_TYPES
        conn = _conn()
        try:
            ctype = (request.form.get('type') or request.args.get('type') or 'indefinite').strip()
            if ctype not in CONTRACT_TYPES:
                return jsonify({'error': 'Loại HĐ không hợp lệ'}), 400
            f = request.files.get('file')
            raw = ''
            if f and f.filename:
                name = (f.filename or '').lower()
                data = f.read()
                if name.endswith(('.html', '.htm', '.txt')):
                    raw = data.decode('utf-8', errors='replace')
                elif name.endswith('.docx'):
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        xml = zf.read('word/document.xml').decode('utf-8', errors='replace')
                    text = re.sub(r'</w:p>', '\n', xml)
                    text = re.sub(r'<[^>]+>', '', text)
                    text = html_mod.unescape(text)
                    if '[[CONTRACT_NO]]' in text and '[[SALARY_TABLE]]' in text:
                        raw = (
                            '<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8"/>'
                            '<title>HĐLĐ</title></head><body><pre style="white-space:pre-wrap;'
                            'font-family:Times New Roman,serif">'
                            + html_mod.escape(text) + '</pre></body></html>'
                        )
                        for k, _ in ld_tpl.KNOWN_PLACEHOLDERS:
                            raw = raw.replace(html_mod.escape(f'[[{k}]]'), f'[[{k}]]')
                    else:
                        return jsonify({
                            'error': 'File .docx thiếu mã [[CONTRACT_NO]] / [[SALARY_TABLE]]. '
                                     'Nên xuất mẫu HTML, sửa rồi Lưu dưới dạng Trang web (.html).',
                        }), 400
                else:
                    return jsonify({'error': 'Chỉ nhận .html / .htm / .txt hoặc .docx có placeholder.'}), 400
            else:
                raw = (request.get_json(silent=True) or {}).get('html') or ''
            ld_tpl.set_custom_template_html(conn, ctype, raw)
            sqlite_commit(conn, label='hrm_ld_tpl_import')
            return jsonify({
                'success': True,
                'placeholders': ld_tpl.extract_placeholders(raw),
            })
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/hrm/shifts')
    @app.route('/SME_hrm_shifts')
    @login_required
    @require_sme_regime
    def SME_hrm_shifts():
        return render_template('hrm/shifts.html')

    @app.route('/hrm/formulas')
    @app.route('/SME_hrm_formulas')
    @login_required
    @require_sme_regime
    def SME_hrm_formulas():
        return render_template('hrm/formulas.html')

    @app.route('/hrm/compliance')
    @app.route('/SME_hrm_compliance')
    @login_required
    @require_sme_regime
    def SME_hrm_compliance():
        return render_template('hrm/compliance.html')

    @app.route('/ess')
    @app.route('/hrm/ess')
    @ess_portal_required
    def hrm_ess_portal():
        return render_template('hrm/ess.html')

    # ── Contracts API ──────────────────────────────────────────────────

    @app.route('/api/hrm/employees/next-code', methods=['GET'])
    @login_required
    def api_hrm_next_employee_code():
        from Services.hrm.employee_codes import next_employee_code
        conn = _conn()
        try:
            return jsonify({'success': True, 'employee_code': next_employee_code(conn)})
        finally:
            conn.close()

    @app.route('/api/hrm/contracts/next-no', methods=['GET'])
    @login_required
    def api_hrm_next_contract_no():
        from Services.hrm.contracts import next_contract_no
        conn = _conn()
        try:
            return jsonify({'success': True, 'contract_no': next_contract_no(conn)})
        finally:
            conn.close()

    @app.route('/api/hrm/contracts/work-defaults', methods=['GET'])
    @login_required
    def api_hrm_contract_work_defaults():
        from Services.hrm.work_calendar import contract_work_defaults
        start = (request.args.get('start_date') or '').strip() or None
        conn = _conn()
        try:
            return jsonify({'success': True, 'data': contract_work_defaults(conn, start)})
        finally:
            conn.close()

    @app.route('/api/hrm/contracts', methods=['GET', 'POST'])
    @login_required
    def api_hrm_contracts():
        from Services.hrm.contracts import CONTRACT_TYPES, list_contracts, upsert_contract
        conn = _conn()
        try:
            if request.method == 'GET':
                status = request.args.get('status', 'active')
                if status == 'all':
                    status = None
                return jsonify({
                    'success': True,
                    'types': CONTRACT_TYPES,
                    'items': list_contracts(conn, status=status),
                    'can_manage_ess_link': _can_manage_ess_link(),
                })
            data = request.get_json(silent=True) or request.form.to_dict()
            item = upsert_contract(conn, data)
            return jsonify({'success': True, 'item': item})
        except Exception as e:
            return _api_error(e)
        finally:
            conn.close()

    @app.route('/api/hrm/contracts/<int:contract_id>', methods=['DELETE'])
    @login_required
    def api_hrm_contract_delete(contract_id):
        from Services.hrm.contracts import delete_contract
        conn = _conn()
        try:
            delete_contract(conn, contract_id)
            return jsonify({'success': True})
        except Exception as e:
            return _api_error(e)
        finally:
            conn.close()

    # ── Shifts / OT API ────────────────────────────────────────────────

    @app.route('/api/hrm/shifts', methods=['GET'])
    @login_required
    def api_hrm_shifts():
        from Services.hrm.shifts import list_ot_policies, list_shifts
        conn = _conn()
        try:
            return jsonify({
                'success': True,
                'shifts': list_shifts(conn),
                'ot_policies': list_ot_policies(conn),
            })
        finally:
            conn.close()

    @app.route('/api/hrm/ot/preview', methods=['POST'])
    @login_required
    def api_hrm_ot_preview():
        from Services.hrm.shifts import preview_ot_line
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            result = preview_ot_line(
                conn,
                base_salary=float(data.get('base_salary') or 0),
                standard_days=float(data.get('standard_days') or 26),
                hours=float(data.get('hours') or 0),
                day_type=data.get('day_type') or 'normal',
                is_night=bool(data.get('is_night')),
            )
            return jsonify({'success': True, **result})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    # ── Formulas API ───────────────────────────────────────────────────

    @app.route('/api/hrm/formulas', methods=['GET', 'POST'])
    @login_required
    def api_hrm_formulas():
        from Services.hrm.formula_engine import evaluate_formula, list_formulas
        from Services.hrm.schema import ensure_hrm_schema
        from db_utils import sqlite_commit
        conn = _conn()
        try:
            ensure_hrm_schema(conn)
            if request.method == 'GET':
                return jsonify({'success': True, 'items': list_formulas(conn)})
            data = request.get_json(silent=True) or {}
            if data.get('test_only'):
                val = evaluate_formula(data.get('expression') or '', data.get('variables') or {})
                return jsonify({'success': True, 'value': val})
            code = (data.get('code') or '').strip().upper()
            if not code:
                return jsonify({'success': False, 'error': 'Thiếu code'}), 400
            existing = conn.execute(
                'SELECT id FROM hrm_payroll_formulas WHERE code=?', (code,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE hrm_payroll_formulas
                    SET name=?, expression=?, output_field=?, is_active=?,
                        version=version+1, updated_at=datetime('now')
                    WHERE code=?
                    """,
                    (
                        data.get('name') or code,
                        data.get('expression') or '',
                        data.get('output_field') or 'bonus',
                        1 if data.get('is_active', True) else 0,
                        code,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO hrm_payroll_formulas (code, name, expression, output_field, is_active)
                    VALUES (?,?,?,?,1)
                    """,
                    (
                        code,
                        data.get('name') or code,
                        data.get('expression') or '',
                        data.get('output_field') or 'bonus',
                    ),
                )
            sqlite_commit(conn, label='hrm_formula')
            return jsonify({'success': True, 'items': list_formulas(conn)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    # ── Compliance ─────────────────────────────────────────────────────

    @app.route('/api/hrm/compliance', methods=['GET', 'POST'])
    @login_required
    def api_hrm_compliance():
        from Services.hrm.compliance import list_open_events, scan_compliance
        conn = _conn()
        try:
            if request.method == 'POST':
                result = scan_compliance(conn)
                return jsonify({'success': True, **result})
            return jsonify({'success': True, 'events': list_open_events(conn)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Caps preview ───────────────────────────────────────────────────

    @app.route('/api/hrm/insurance-caps', methods=['GET'])
    @login_required
    def api_hrm_insurance_caps():
        from Services.hrm.insurance_cap import apply_insurance_caps, get_cap_config
        conn = _conn()
        try:
            cfg = get_cap_config(conn)
            sample = apply_insurance_caps(
                conn,
                insurance_salary=request.args.get('salary', type=float),
                base_salary=request.args.get('salary', type=float),
            )
            return jsonify({'success': True, 'config': cfg, 'sample': sample})
        finally:
            conn.close()

    # ── Export BHXH ────────────────────────────────────────────────────

    @app.route('/api/hrm/export/bhxh.csv')
    @login_required
    def api_hrm_export_bhxh():
        from Services.hrm.exports import export_bhxh_csv
        month = request.args.get('month', type=int) or 1
        year = request.args.get('year', type=int) or 2026
        conn = _conn()
        try:
            csv_text = export_bhxh_csv(conn, month, year)
            return Response(
                csv_text,
                mimetype='text/csv; charset=utf-8',
                headers={
                    'Content-Disposition': f'attachment; filename=bhxh_{month:02d}_{year}.csv'
                },
            )
        finally:
            conn.close()

    # ── Device import adapter ──────────────────────────────────────────

    @app.route('/api/hrm/attendance/import-device', methods=['POST'])
    @login_required
    def api_hrm_import_device():
        from Services.attendance_helpers import upsert_attendance_log
        from Services.hrm.exports import parse_device_attlog
        data = request.get_json(silent=True) or {}
        brand = data.get('brand') or 'hikvision'
        raw = data.get('raw') or ''
        sn = data.get('device_sn') or brand.upper()
        conn = _conn()
        try:
            parsed = parse_device_attlog(brand, raw)
            ok = 0
            for item in parsed:
                good, _ = upsert_attendance_log(conn, item, source=brand, device_sn=sn)
                if good:
                    ok += 1
            sqlite_commit(conn, label='hrm_device_import')
            return jsonify({'success': True, 'imported': ok, 'parsed': len(parsed)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    # ── Effective salary ───────────────────────────────────────────────

    @app.route('/api/hrm/salary-effective', methods=['POST'])
    @login_required
    def api_hrm_salary_effective():
        from Services.hrm.effective_salary import set_effective_salary
        from Services.hrm.exports import emit_webhook
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            item = set_effective_salary(conn, data)
            try:
                emit_webhook(conn, 'salary.effective_changed', item)
            except Exception:
                pass
            return jsonify({'success': True, 'item': item})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    # ── ESS ────────────────────────────────────────────────────────────

    @app.route('/api/hrm/ess/me', methods=['GET'])
    @ess_portal_required
    def api_hrm_ess_me():
        from Services.hrm.ess import employee_payslips, list_leave
        from Services.hrm.ess_access import EssAccessDenied, resolve_ess_employee
        conn = _conn()
        try:
            try:
                emp = resolve_ess_employee(conn, user_id=session.get('user_id'))
            except EssAccessDenied as exc:
                return jsonify({'success': False, 'error': str(exc)}), 403
            eid = int(emp['id'])
            return jsonify({
                'success': True,
                'employee': {
                    'id': eid,
                    'fullname': emp.get('fullname'),
                    'position': emp.get('position'),
                    'department': emp.get('department'),
                },
                'payslips': employee_payslips(conn, eid),
                'leaves': list_leave(conn, eid),
            })
        finally:
            conn.close()

    @app.route('/api/hrm/ess/leave', methods=['POST'])
    @ess_portal_required
    def api_hrm_ess_leave():
        from Services.hrm.ess import create_leave_request
        from Services.hrm.ess_access import EssAccessDenied, bind_ess_employee_id, resolve_ess_employee
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            try:
                emp = resolve_ess_employee(conn, user_id=session.get('user_id'))
            except EssAccessDenied as exc:
                return _api_error(exc, status=403)
            data = bind_ess_employee_id(data, emp)
            item = create_leave_request(conn, data)
            return jsonify({'success': True, 'item': item})
        except Exception as e:
            return _api_error(e)
        finally:
            conn.close()

    @app.route('/api/hrm/ess/checkin', methods=['POST'])
    @ess_portal_required
    def api_hrm_ess_checkin():
        from Services.hrm.ess import mobile_checkin
        from Services.hrm.ess_access import EssAccessDenied, bind_ess_employee_id, resolve_ess_employee
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            try:
                emp = resolve_ess_employee(conn, user_id=session.get('user_id'))
            except EssAccessDenied as exc:
                return _api_error(exc, status=403)
            data = bind_ess_employee_id(data, emp)
            item = mobile_checkin(conn, data)
            return jsonify({'success': True, 'item': item})
        except Exception as e:
            return _api_error(e)
        finally:
            conn.close()

    @app.route('/api/hrm/ess/visit-customers', methods=['GET'])
    @ess_portal_required
    def api_hrm_ess_visit_customers():
        from Services.crm_visits import list_visit_customers
        from Services.hrm.ess_access import EssAccessDenied, resolve_ess_employee
        conn = _conn()
        try:
            try:
                resolve_ess_employee(conn, user_id=session.get('user_id'))
            except EssAccessDenied as exc:
                return jsonify({'success': False, 'error': str(exc)}), 403
            owner = (
                session.get('username')
                or (session.get('user') or {}).get('username')
                or ''
            ).strip()
            if not owner:
                return jsonify({'success': False, 'error': 'Không xác định tài khoản'}), 400
            today_only = request.args.get('today_only') == '1'
            items = list_visit_customers(conn, owner, include_all=not today_only)
            return jsonify({'success': True, 'owner': owner, 'items': items})
        finally:
            conn.close()

    @app.route('/api/hrm/ess/visit-checkin', methods=['POST'])
    @ess_portal_required
    def api_hrm_ess_visit_checkin():
        from Services.crm_visits import visit_checkin
        from Services.hrm.ess_access import EssAccessDenied, resolve_ess_employee
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            try:
                emp = resolve_ess_employee(conn, user_id=session.get('user_id'))
            except EssAccessDenied as exc:
                return jsonify({'success': False, 'error': str(exc)}), 403
            owner = (
                session.get('username')
                or (session.get('user') or {}).get('username')
                or ''
            ).strip()
            item = visit_checkin(
                conn,
                data,
                owner=owner,
                employee_id=int(emp.get('id') or 0) or None,
            )
            return jsonify({'success': True, 'item': item})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/hrm/ess/visit-log', methods=['GET'])
    @ess_portal_required
    def api_hrm_ess_visit_log():
        from Services.crm_visits import list_visits
        from Services.hrm.ess_access import EssAccessDenied, resolve_ess_employee
        conn = _conn()
        try:
            try:
                resolve_ess_employee(conn, user_id=session.get('user_id'))
            except EssAccessDenied as exc:
                return jsonify({'success': False, 'error': str(exc)}), 403
            owner = (
                session.get('username')
                or (session.get('user') or {}).get('username')
                or ''
            ).strip()
            items = list_visits(
                conn,
                owner=owner,
                visit_date=request.args.get('date') or None,
                limit=int(request.args.get('limit') or 30),
            )
            return jsonify({'success': True, 'items': items})
        finally:
            conn.close()

    @app.route('/api/hrm/ess/link', methods=['POST'])
    @login_required
    def api_hrm_ess_link():
        """HR gán user ↔ NV và bật ESS (ess_enabled)."""
        from Services.hrm.ess_access import (
            link_employee_ess,
            session_may_manage_ess_link,
            unlink_employee_ess,
        )
        if not session_may_manage_ess_link():
            return jsonify({'success': False, 'error': 'Forbidden'}), 403
        data = request.get_json(silent=True) or {}
        try:
            employee_id = int(data.get('employee_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Thiếu employee_id'}), 400
        if not employee_id:
            return jsonify({'success': False, 'error': 'Thiếu employee_id'}), 400
        conn = _conn()
        try:
            if data.get('unlink'):
                unlink_employee_ess(conn, employee_id)
                return jsonify({'success': True, 'message': 'Đã gỡ liên kết ESS'})
            try:
                user_id = int(data.get('user_id') or 0)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Thiếu user_id'}), 400
            if not user_id:
                return jsonify({'success': False, 'error': 'Chọn tài khoản đăng nhập'}), 400
            link_employee_ess(
                conn,
                employee_id,
                user_id,
                enable=bool(data.get('ess_enabled', True)),
            )
            urow = conn.execute(
                'SELECT username, full_name FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            username = ''
            if urow:
                username = (urow['username'] if hasattr(urow, 'keys') else urow[0]) or ''
            return jsonify({
                'success': True,
                'message': 'Đã lưu thiết lập ESS',
                'username': username,
                'ess_url': '/hrm/ess',
                'ess_enabled': bool(data.get('ess_enabled', True)),
            })
        except Exception as e:
            return _api_error(e)
        finally:
            conn.close()

    @app.route('/api/hrm/ess/linkable-users', methods=['GET'])
    @login_required
    def api_hrm_ess_linkable_users():
        from Services.hrm.ess_access import list_ess_linkable_users, session_may_manage_ess_link
        if not session_may_manage_ess_link():
            return jsonify({'success': False, 'error': 'Forbidden'}), 403
        employee_id = request.args.get('employee_id', type=int)
        conn = _conn()
        try:
            return jsonify({
                'success': True,
                'items': list_ess_linkable_users(conn, employee_id=employee_id),
            })
        finally:
            conn.close()

    @app.route('/api/hrm/bank/encrypt', methods=['POST'])
    @login_required
    def api_hrm_bank_encrypt():
        from Services.hrm.encryption import store_employee_bank
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            store_employee_bank(conn, int(data.get('employee_id') or 0), data.get('account') or '')
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()
