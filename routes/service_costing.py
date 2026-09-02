"""Routes API + trang Giá vốn dịch vụ SME (TT99)."""
from __future__ import annotations

import logging

from flask import jsonify, request
from flask_login import current_user, login_required

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def _ensure_service_costing_schema(conn, *, commit: bool = False) -> None:
    from Services.schema_cache import ensure_schema_once
    from Services.sme.service_costing import ensure_service_costing_schema
    ensure_schema_once(
        conn, 'service_costing', ensure_service_costing_schema, commit=commit,
    )


def _username() -> str:
    try:
        return getattr(current_user, 'username', '') or ''
    except Exception:
        return ''


def register_service_costing_routes(app):
    @app.route('/api/sme/service-costing/departments')
    @login_required
    def api_service_costing_departments():
        from Services.sme.service_costing import list_service_departments
        return jsonify({'success': True, 'data': list_service_departments()})

    @app.route('/api/sme/service-costing/products')
    @login_required
    def api_service_costing_products():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_service_products,
            )
            _ensure_service_costing_schema(conn, commit=True)
            return jsonify({
                'success': True,
                'data': list_service_products(conn, request.args.get('q', '')),
            })
        except Exception as exc:
            logger.exception('service products: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs')
    @login_required
    def api_service_costing_jobs_list():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_service_jobs,
            )
            _ensure_service_costing_schema(conn, commit=True)
            rows = list_service_jobs(
                conn,
                date_from=request.args.get('from', ''),
                date_to=request.args.get('to', ''),
                status=request.args.get('status', ''),
                q=request.args.get('q', ''),
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('service jobs list: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs/<int:job_id>')
    @login_required
    def api_service_costing_job_get(job_id):
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                get_service_job,
            )
            _ensure_service_costing_schema(conn, commit=True)
            job = get_service_job(conn, job_id)
            if not job:
                return jsonify({'success': False, 'error': 'Không tìm thấy lệnh'}), 404
            return jsonify({'success': True, 'data': job})
        except Exception as exc:
            logger.exception('service job get: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs', methods=['POST'])
    @login_required
    def api_service_costing_job_create():
        data = request.get_json(silent=True) or {}
        try:
            pid = int(data.get('service_product_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Dịch vụ không hợp lệ'}), 400
        if pid <= 0:
            return jsonify({'success': False, 'error': 'Chọn dịch vụ'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import create_service_job
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            job = create_service_job(
                conn,
                service_product_id=pid,
                job_date=data.get('job_date'),
                qty=float(data.get('qty') or 1),
                customer_id=int(data['customer_id']) if data.get('customer_id') else None,
                customer_name=data.get('customer_name') or '',
                department=data.get('department') or '',
                labor_cost=float(data.get('labor_cost') or 0),
                note=data.get('note') or '',
                sale_id=int(data['sale_id']) if data.get('sale_id') else None,
                apply_norms=bool(data.get('apply_norms', True)),
                created_by=_username(),
                commit=True,
            )
            msg = f"Đã lập lệnh {job['voucher_no']}"
            applied = job.get('norms_applied')
            if applied and applied.get('lines'):
                msg += f" · đã áp định mức ({len(applied['lines'])} dòng CP)"
            return jsonify({
                'success': True,
                'data': job,
                'message': msg,
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service job create: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs/<int:job_id>/costs', methods=['POST'])
    @login_required
    def api_service_costing_add_cost(job_id):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import add_service_job_cost
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            job = add_service_job_cost(
                conn, job_id,
                cost_type=data.get('cost_type') or 'overhead',
                amount=float(data.get('amount') or 0),
                cost_date=data.get('cost_date'),
                description=data.get('description') or '',
                in_norm=bool(data.get('in_norm', True)),
                product_id=int(data['product_id']) if data.get('product_id') else None,
                qty=float(data['qty']) if data.get('qty') not in (None, '') else None,
                unit_cost=float(data['unit_cost']) if data.get('unit_cost') not in (None, '') else None,
                credit_account=data.get('credit_account') or None,
                debit_collect_account=data.get('debit_collect_account') or None,
                source_type=data.get('source_type') or 'manual',
                source_id=int(data['source_id']) if data.get('source_id') else None,
                auto_post=bool(data.get('auto_post', True)),
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': job,
                'message': 'Đã ghi chi phí và hạch toán vào 154 (hoặc 6323 nếu vượt mức)',
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service cost add: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs/<int:job_id>/collect', methods=['POST'])
    @login_required
    def api_service_costing_collect(job_id):
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import collect_job_to_wip
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            job = collect_job_to_wip(conn, job_id, created_by=_username(), commit=True)
            return jsonify({'success': True, 'data': job, 'message': 'Đã tập hợp CP vào 154'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service collect: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs/<int:job_id>/deliver', methods=['POST'])
    @login_required
    def api_service_costing_deliver(job_id):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import deliver_service_job
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            job = deliver_service_job(
                conn, job_id,
                deliver_date=data.get('deliver_date') or data.get('date'),
                sale_id=int(data['sale_id']) if data.get('sale_id') else None,
                percent=float(data['percent']) if data.get('percent') not in (None, '') else None,
                amount=float(data['amount']) if data.get('amount') not in (None, '') else None,
                note=data.get('note') or '',
                created_by=_username(),
                commit=True,
            )
            j = job.get('deliver_journal') or {}
            amt = job.get('last_delivery_amount') or j.get('amount') or 0
            msg = f"Đã nghiệm thu {job.get('voucher_no')} — kết chuyển {amt:,.0f} ₫"
            if j.get('entry_no'):
                msg += f" · sổ {j['entry_no']} (Nợ 6323 / Có 154)"
            if job.get('status') == 'partial_delivered':
                msg += f" · còn dở dang {float(job.get('wip_balance') or 0):,.0f} ₫ trên tài khoản 154"
            return jsonify({'success': True, 'data': job, 'message': msg})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service deliver: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs/<int:job_id>/cancel', methods=['POST'])
    @login_required
    def api_service_costing_cancel(job_id):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import cancel_service_job
            job = cancel_service_job(
                conn, job_id,
                reason=data.get('reason') or data.get('note') or '',
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': job,
                'message': f"Đã hủy {job.get('voucher_no')}",
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service cancel: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/allocate', methods=['POST'])
    @login_required
    def api_service_costing_allocate():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import allocate_overhead
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            job_ids = data.get('job_ids') or None
            if job_ids is not None:
                job_ids = [int(x) for x in job_ids]
            result = allocate_overhead(
                conn,
                alloc_date=data.get('alloc_date'),
                total_amount=float(data.get('total_amount') or 0),
                credit_account=data.get('credit_account') or '1111',
                description=data.get('description') or '',
                basis=data.get('basis') or 'qty',
                job_ids=job_ids,
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': result,
                'message': f"Đã phân bổ {result['total_amount']:,.0f} ₫ vào {len(result['lines'])} lệnh",
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service allocate: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/standards', methods=['GET'])
    @login_required
    def api_service_costing_standards_list():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_service_cost_standards,
            )
            _ensure_service_costing_schema(conn, commit=True)
            return jsonify({'success': True, 'data': list_service_cost_standards(conn)})
        except Exception as exc:
            logger.exception('service standards list: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/standards/<int:product_id>', methods=['GET'])
    @login_required
    def api_service_costing_standard_get(product_id):
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                get_service_cost_standard,
                preview_service_cost_standard,
            )
            _ensure_service_costing_schema(conn, commit=True)
            std = get_service_cost_standard(conn, product_id)
            qty = request.args.get('qty')
            preview = None
            if qty not in (None, ''):
                preview = preview_service_cost_standard(
                    conn, product_id, qty=float(qty),
                )
            return jsonify({
                'success': True,
                'data': std,
                'preview': preview,
                'has_standard': bool(std),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service standard get: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/standards', methods=['POST'])
    @login_required
    def api_service_costing_standard_save():
        data = request.get_json(silent=True) or {}
        try:
            pid = int(data.get('service_product_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Dịch vụ không hợp lệ'}), 400
        if pid <= 0:
            return jsonify({'success': False, 'error': 'Chọn dịch vụ'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import save_service_cost_standard
            std = save_service_cost_standard(
                conn,
                service_product_id=pid,
                labor_std_per_unit=float(data.get('labor_std_per_unit') or 0),
                oh_fixed_std_per_unit=float(data.get('oh_fixed_std_per_unit') or 0),
                oh_variable_std_per_unit=float(data.get('oh_variable_std_per_unit') or 0),
                outsource_std_per_unit=float(data.get('outsource_std_per_unit') or 0),
                note=data.get('note') or '',
                materials=data.get('materials') or [],
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': std,
                'message': f"Đã lưu định mức dịch vụ {std.get('service_name') or pid}",
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service standard save: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/standards/<int:product_id>', methods=['DELETE'])
    @login_required
    def api_service_costing_standard_delete(product_id):
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import delete_service_cost_standard
            delete_service_cost_standard(conn, product_id, commit=True)
            return jsonify({'success': True, 'message': 'Đã xóa định mức'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service standard delete: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/standards/preview', methods=['POST'])
    @login_required
    def api_service_costing_standard_preview():
        data = request.get_json(silent=True) or {}
        try:
            pid = int(data.get('service_product_id') or 0)
            qty = float(data.get('qty') or 1)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tham số không hợp lệ'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import preview_service_cost_standard
            prev = preview_service_cost_standard(conn, pid, qty=qty)
            return jsonify({'success': True, 'data': prev})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service standard preview: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/jobs/<int:job_id>/apply-standard', methods=['POST'])
    @login_required
    def api_service_costing_apply_standard(job_id):
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import apply_service_cost_standard
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            result = apply_service_cost_standard(
                conn, job_id, created_by=_username(), commit=True,
            )
            from Services.sme.service_costing import get_service_job
            job = get_service_job(conn, job_id)
            return jsonify({
                'success': True,
                'data': job,
                'applied': result,
                'message': f"Đã áp định mức — {len(result.get('lines') or [])} dòng CP",
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service apply standard: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/outsource/provisionals')
    @login_required
    def api_service_outsource_provisionals():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_outsource_provisionals,
            )
            _ensure_service_costing_schema(conn, commit=True)
            unmatched = str(request.args.get('unmatched', '1')).lower() not in (
                '0', 'false', 'no',
            )
            rows = list_outsource_provisionals(conn, unmatched_only=unmatched)
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('outsource provisionals: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/outsource/provisional', methods=['POST'])
    @login_required
    def api_service_outsource_provisional_add():
        data = request.get_json(silent=True) or {}
        try:
            job_id = int(data.get('job_id') or 0)
            amount = float(data.get('amount') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tham số không hợp lệ'}), 400
        if job_id <= 0:
            return jsonify({'success': False, 'error': 'Chọn lệnh dịch vụ'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import add_outsource_provisional
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            job = add_outsource_provisional(
                conn, job_id,
                amount=amount,
                cost_date=data.get('cost_date') or data.get('date'),
                vendor_name=data.get('vendor_name') or '',
                description=data.get('description') or '',
                credit_account=data.get('credit_account') or '331',
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': job,
                'message': 'Đã ghi thuê ngoài dự kiến vào 154 (Nợ 627 / Có 331)',
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('outsource provisional: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/outsource/invoices')
    @login_required
    def api_service_outsource_invoices():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_outsource_invoices,
            )
            _ensure_service_costing_schema(conn, commit=True)
            unassigned = str(request.args.get('unassigned', '1')).lower() not in (
                '0', 'false', 'no',
            )
            rows = list_outsource_invoices(
                conn,
                date_from=request.args.get('from', ''),
                date_to=request.args.get('to', ''),
                q=request.args.get('q', ''),
                unassigned_only=unassigned,
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('outsource invoices: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/outsource/assignments')
    @login_required
    def api_service_outsource_assignments():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_outsource_assignments,
            )
            _ensure_service_costing_schema(conn, commit=True)
            inv = request.args.get('invoice_id')
            job = request.args.get('job_id')
            rows = list_outsource_assignments(
                conn,
                invoice_id=int(inv) if inv else None,
                job_id=int(job) if job else None,
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('outsource assignments: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/outsource/assign', methods=['POST'])
    @login_required
    def api_service_outsource_assign():
        data = request.get_json(silent=True) or {}
        try:
            invoice_id = int(data.get('invoice_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Hóa đơn không hợp lệ'}), 400
        if invoice_id <= 0:
            return jsonify({'success': False, 'error': 'Chọn hóa đơn NCC'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import assign_outsource_invoice
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            result = assign_outsource_invoice(
                conn,
                invoice_id=invoice_id,
                allocations=data.get('allocations') or [],
                assign_date=data.get('assign_date') or data.get('date'),
                note=data.get('note') or '',
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': result,
                'message': (
                    f"Đã gán HĐ {result.get('invoice_no')} — "
                    f"{result.get('assigned_now', 0):,.0f} ₫ vào "
                    f"{len(result.get('lines') or [])} lệnh"
                ),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('outsource assign: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/advances/receipts')
    @login_required
    def api_service_advance_receipts():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_advance_receipts,
            )
            _ensure_service_costing_schema(conn, commit=True)
            job_id = request.args.get('job_id')
            rows = list_advance_receipts(
                conn,
                customer_name=request.args.get('customer') or request.args.get('q'),
                unassigned_only=str(request.args.get('unassigned', '1')).lower() not in (
                    '0', 'false', 'no',
                ),
                include_job_id=int(job_id) if job_id else None,
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('service advance receipts: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/advances/bank-txns')
    @login_required
    def api_service_advance_bank_txns():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_unmatched_bank_inflows,
            )
            _ensure_service_costing_schema(conn, commit=True)
            rows = list_unmatched_bank_inflows(
                conn,
                date_from=request.args.get('from', ''),
                date_to=request.args.get('to', ''),
                q=request.args.get('q', ''),
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('service advance bank txns: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/advances/record', methods=['POST'])
    @login_required
    def api_service_advance_record():
        data = request.get_json(silent=True) or {}
        try:
            job_id = int(data.get('job_id') or 0)
            amount = float(data.get('amount') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tham số không hợp lệ'}), 400
        if job_id <= 0:
            return jsonify({'success': False, 'error': 'Chọn lệnh dịch vụ'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import record_service_advance
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            job = record_service_advance(
                conn, job_id,
                amount=amount,
                voucher_date=data.get('voucher_date') or data.get('date'),
                payment_method=data.get('payment_method') or 'bank',
                credit_account=data.get('credit_account') or '131',
                party_name=data.get('party_name') or data.get('customer_name') or '',
                reason=data.get('reason') or '',
                note=data.get('note') or '',
                created_by=_username(),
                commit=True,
            )
            vno = (job.get('advance_voucher') or {}).get('voucher_no') or ''
            return jsonify({
                'success': True,
                'data': job,
                'message': f'Đã lập PT {vno} và gắn lệnh {job.get("voucher_no")}',
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service advance record: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/advances/assign-receipt', methods=['POST'])
    @login_required
    def api_service_advance_assign_receipt():
        data = request.get_json(silent=True) or {}
        try:
            voucher_id = int(data.get('voucher_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Chọn phiếu thu'}), 400
        if voucher_id <= 0:
            return jsonify({'success': False, 'error': 'Chọn phiếu thu'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import assign_advance_receipt
            result = assign_advance_receipt(
                conn,
                voucher_id=voucher_id,
                allocations=data.get('allocations') or [],
                assign_date=data.get('assign_date') or data.get('date'),
                note=data.get('note') or '',
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': result,
                'message': (
                    f"Đã gán PT {result.get('voucher_no')} — "
                    f"{result.get('assigned_now', 0):,.0f} ₫ vào "
                    f"{len(result.get('lines') or [])} lệnh"
                ),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service advance assign receipt: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/advances/assign-bank', methods=['POST'])
    @login_required
    def api_service_advance_assign_bank():
        data = request.get_json(silent=True) or {}
        try:
            bank_txn_id = int(data.get('bank_txn_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Chọn giao dịch NH'}), 400
        if bank_txn_id <= 0:
            return jsonify({'success': False, 'error': 'Chọn giao dịch NH'}), 400
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.service_costing import assign_bank_txn_to_jobs
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            result = assign_bank_txn_to_jobs(
                conn,
                bank_txn_id=bank_txn_id,
                allocations=data.get('allocations') or [],
                credit_account=data.get('credit_account') or '131',
                assign_date=data.get('assign_date') or data.get('date'),
                note=data.get('note') or '',
                created_by=_username(),
                commit=True,
            )
            vno = ((result.get('voucher') or {}).get('voucher_no')) or ''
            return jsonify({
                'success': True,
                'data': result,
                'message': (
                    f"Đã gán GD NH — {result.get('assigned_now', 0):,.0f} ₫"
                    + (f' (PT {vno})' if vno else '')
                ),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('service advance assign bank: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/service-costing/advances/assignments')
    @login_required
    def api_service_advance_assignments():
        conn = get_db_connection()
        try:
            from Services.sme.service_costing import (
                _ensure_service_costing_schema,
                list_service_advance_assignments,
            )
            _ensure_service_costing_schema(conn, commit=True)
            job_id = request.args.get('job_id')
            voucher_id = request.args.get('voucher_id')
            rows = list_service_advance_assignments(
                conn,
                job_id=int(job_id) if job_id else None,
                voucher_id=int(voucher_id) if voucher_id else None,
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('service advance assignments: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/materials-for-service')
    @login_required
    def api_materials_for_service():
        conn = get_db_connection()
        try:
            from Services.production_costing import (
                ensure_production_schema,
                list_material_products,
            )
            ensure_production_schema(conn)
            return jsonify({
                'success': True,
                'data': list_material_products(
                    conn,
                    request.args.get('q', ''),
                    include_fg_goods=True,
                ),
            })
        except Exception as exc:
            logger.exception('materials for service: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()
