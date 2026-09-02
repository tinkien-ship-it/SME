"""API Phân bổ giá thành cuối kỳ 622/627 — TT99."""
from __future__ import annotations

import logging

from flask import jsonify, request
from flask_login import current_user, login_required

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def _username() -> str:
    try:
        return getattr(current_user, 'username', '') or ''
    except Exception:
        return ''


def register_period_cost_allocation_routes(app):
    @app.route('/api/sme/costing-settings', methods=['GET'])
    @login_required
    def api_costing_settings_get():
        conn = get_db_connection()
        try:
            from Services.sme.costing_policy import ensure_costing_policy_schema, get_costing_policy
            ensure_costing_policy_schema(conn, commit=True)
            return jsonify({'success': True, 'data': get_costing_policy(conn)})
        except Exception as exc:
            logger.exception('costing settings get: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-settings', methods=['POST'])
    @login_required
    def api_costing_settings_save():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.costing_policy import save_costing_policy
            settings = save_costing_policy(
                conn,
                allocation_method=data.get('allocation_method') or 'normal_capacity',
                normal_capacity_month=float(data.get('normal_capacity_month') or 500),
                working_days_month=float(data.get('working_days_month') or 25),
                require_finalize_before_fg=bool(data.get('require_finalize_before_fg', False)),
                department_name=data.get('department_name') or 'Bộ phận sản xuất',
                auto_close=bool(data.get('auto_close', False)),
                commit=True,
            )
            return jsonify({'success': True, 'data': settings, 'message': 'Đã lưu chính sách giá thành kỳ'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('costing settings save: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-settings/auto-close', methods=['POST'])
    @login_required
    def api_costing_auto_close_toggle():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.costing_policy import set_auto_close
            settings = set_auto_close(conn, bool(data.get('auto_close')), commit=True)
            return jsonify({
                'success': True,
                'data': settings,
                'message': 'Đã bật chốt tự động cuối tháng' if settings.get('auto_close')
                else 'Đã chuyển sang chốt thủ công',
            })
        except Exception as exc:
            logger.exception('auto close toggle: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-settings/unlock', methods=['POST'])
    @login_required
    def api_costing_unlock():
        conn = get_db_connection()
        try:
            from Services.sme.costing_policy import unlock_policy_if_no_orders
            settings = unlock_policy_if_no_orders(conn, commit=True)
            return jsonify({'success': True, 'data': settings, 'message': 'Đã mở khóa chính sách'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-factors')
    @login_required
    def api_costing_factors_list():
        conn = get_db_connection()
        try:
            from Services.sme.period_cost_allocation import (
                ensure_period_cost_allocation_schema,
                list_finished_product_factors,
            )
            ensure_period_cost_allocation_schema(conn, commit=True)
            return jsonify({'success': True, 'data': list_finished_product_factors(conn)})
        except Exception as exc:
            logger.exception('costing factors: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-factors/<int:product_id>', methods=['POST'])
    @login_required
    def api_costing_factor_save(product_id):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.period_cost_allocation import set_product_equivalent_factor
            set_product_equivalent_factor(
                conn, product_id, float(data.get('factor') or 1), commit=True,
            )
            return jsonify({'success': True, 'message': 'Đã cập nhật hệ số quy đổi'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('costing factor save: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-cost-allocation/orders')
    @login_required
    def api_pca_orders():
        conn = get_db_connection()
        try:
            from Services.sme.period_cost_allocation import list_orders_for_allocation
            rows = list_orders_for_allocation(
                conn,
                date_from=request.args.get('from') or request.args.get('date_from') or '',
                date_to=request.args.get('to') or request.args.get('date_to') or '',
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('pca orders: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-cost-allocation/preview', methods=['POST'])
    @login_required
    def api_pca_preview():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.period_cost_allocation import preview_allocation
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            order_ids = data.get('order_ids')
            if order_ids is not None:
                order_ids = [int(x) for x in order_ids]
            result = preview_allocation(
                conn,
                fiscal_year=int(data.get('fiscal_year') or data.get('year') or 0),
                period=int(data.get('period') or data.get('month') or 0),
                date_from=data.get('date_from') or data.get('from'),
                date_to=data.get('date_to') or data.get('to'),
                labor_amount=float(data.get('labor_amount') or 0),
                oh_fixed_amount=float(data.get('oh_fixed_amount') or 0),
                oh_variable_amount=float(data.get('oh_variable_amount') or 0),
                allocation_method=data.get('allocation_method'),
                normal_capacity_month=(
                    float(data['normal_capacity_month'])
                    if data.get('normal_capacity_month') not in (None, '') else None
                ),
                working_days_month=(
                    float(data['working_days_month'])
                    if data.get('working_days_month') not in (None, '') else None
                ),
                order_ids=order_ids,
            )
            return jsonify({'success': True, 'data': result})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('pca preview: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-cost-allocation/post', methods=['POST'])
    @login_required
    def api_pca_post():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.period_cost_allocation import post_allocation
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            order_ids = data.get('order_ids')
            if order_ids is not None:
                order_ids = [int(x) for x in order_ids]
            result = post_allocation(
                conn,
                fiscal_year=int(data.get('fiscal_year') or data.get('year') or 0),
                period=int(data.get('period') or data.get('month') or 0),
                date_from=data.get('date_from') or data.get('from'),
                date_to=data.get('date_to') or data.get('to'),
                labor_amount=float(data.get('labor_amount') or 0),
                oh_fixed_amount=float(data.get('oh_fixed_amount') or 0),
                oh_variable_amount=float(data.get('oh_variable_amount') or 0),
                allocation_method=data.get('allocation_method'),
                normal_capacity_month=(
                    float(data['normal_capacity_month'])
                    if data.get('normal_capacity_month') not in (None, '') else None
                ),
                working_days_month=(
                    float(data['working_days_month'])
                    if data.get('working_days_month') not in (None, '') else None
                ),
                order_ids=order_ids,
                note=data.get('note') or '',
                close_idle_now=bool(data.get('close_idle_now')),
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': result,
                'message': (
                    f"Đã phân bổ #{result['id']}: cập nhật giá thành lệnh và ghi sổ. "
                    f"Dưới công suất tạm: {float(result.get('labor_idle') or 0) + float(result.get('oh_fixed_idle') or 0):,.0f} ₫"
                ),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('pca post: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-cost-allocation')
    @login_required
    def api_pca_list():
        conn = get_db_connection()
        try:
            from Services.sme.period_cost_allocation import list_allocations
            year = request.args.get('year')
            period = request.args.get('period')
            rows = list_allocations(
                conn,
                fiscal_year=int(year) if year else None,
                period=int(period) if period else None,
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as exc:
            logger.exception('pca list: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-cost-allocation/<int:alloc_id>')
    @login_required
    def api_pca_get(alloc_id):
        conn = get_db_connection()
        try:
            from Services.sme.period_cost_allocation import get_allocation
            row = get_allocation(conn, alloc_id)
            if not row:
                return jsonify({'success': False, 'error': 'Không tìm thấy'}), 404
            return jsonify({'success': True, 'data': row})
        except Exception as exc:
            logger.exception('pca get: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-cost-allocation/<int:alloc_id>/close-idle', methods=['POST'])
    @login_required
    def api_pca_close_idle(alloc_id):
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.period_cost_allocation import close_allocation_idle
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            row = close_allocation_idle(conn, alloc_id, created_by=_username(), commit=True)
            return jsonify({
                'success': True,
                'data': row,
                'message': 'Đã kết chuyển chi phí dưới công suất sang tài khoản 632',
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('pca close idle: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-cost-allocation/<int:alloc_id>/reverse', methods=['POST'])
    @login_required
    def api_pca_reverse(alloc_id):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.period_cost_allocation import reverse_allocation
            row = reverse_allocation(
                conn, alloc_id,
                created_by=_username(),
                reason=data.get('reason') or '',
                commit=True,
            )
            return jsonify({'success': True, 'data': row, 'message': 'Đã đảo phân bổ'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('pca reverse: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    # ---- Định mức theo phương án ----
    @app.route('/api/sme/cost-standards/<method>')
    @login_required
    def api_cost_standards_list(method):
        conn = get_db_connection()
        try:
            from Services.sme.product_cost_standards import (
                ensure_product_cost_standards_schema,
                list_standards,
            )
            ensure_product_cost_standards_schema(conn, commit=True)
            return jsonify({'success': True, 'data': list_standards(conn, method)})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('cost standards list: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cost-standards/<method>/<int:product_id>')
    @login_required
    def api_cost_standard_get(method, product_id):
        conn = get_db_connection()
        try:
            from Services.sme.product_cost_standards import get_standard
            row = get_standard(conn, method, product_id)
            if not row:
                return jsonify({'success': False, 'error': 'Chưa có định mức'}), 404
            return jsonify({'success': True, 'data': row})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cost-standards/<method>', methods=['POST'])
    @login_required
    def api_cost_standard_save(method):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.product_cost_standards import save_standard
            row = save_standard(
                conn,
                allocation_method=method,
                finished_product_id=int(data.get('finished_product_id') or 0),
                labor_std_per_unit=float(data.get('labor_std_per_unit') or 0),
                oh_fixed_std_per_unit=float(data.get('oh_fixed_std_per_unit') or 0),
                oh_variable_std_per_unit=float(data.get('oh_variable_std_per_unit') or 0),
                equivalent_factor=float(data.get('equivalent_factor') or 1),
                note=data.get('note') or '',
                materials=data.get('materials') or [],
                commit=True,
            )
            return jsonify({'success': True, 'data': row, 'message': 'Đã lưu định mức'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('cost standard save: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cost-standards/<method>/<int:product_id>', methods=['DELETE'])
    @login_required
    def api_cost_standard_delete(method, product_id):
        conn = get_db_connection()
        try:
            from Services.sme.product_cost_standards import delete_standard
            delete_standard(conn, method, product_id, commit=True)
            return jsonify({'success': True, 'message': 'Đã xóa định mức'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cost-standards/preview', methods=['POST'])
    @login_required
    def api_cost_standard_preview():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.costing_policy import get_costing_policy
            from Services.sme.product_cost_standards import preview_order_from_standard
            method = data.get('allocation_method') or get_costing_policy(conn)['allocation_method']
            result = preview_order_from_standard(
                conn,
                allocation_method=method,
                finished_product_id=int(data.get('finished_product_id') or 0),
                qty=float(data.get('qty') or 0),
            )
            return jsonify({'success': True, 'data': result})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('cost standard preview: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    # ---- Chốt kỳ ----
    @app.route('/api/sme/costing-period-close/preview', methods=['POST'])
    @login_required
    def api_costing_close_preview():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.costing_period_close import preview_period_close
            result = preview_period_close(
                conn,
                fiscal_year=int(data.get('fiscal_year') or data.get('year') or 0),
                period=int(data.get('period') or data.get('month') or 0),
                labor_amount=(
                    float(data['labor_amount']) if data.get('labor_amount') not in (None, '') else None
                ),
                oh_fixed_amount=(
                    float(data['oh_fixed_amount']) if data.get('oh_fixed_amount') not in (None, '') else None
                ),
                oh_variable_amount=(
                    float(data['oh_variable_amount'])
                    if data.get('oh_variable_amount') not in (None, '') else None
                ),
                cost_reductions=float(data.get('cost_reductions') or 0),
            )
            return jsonify({'success': True, 'data': result})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('close preview: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-period-close/post', methods=['POST'])
    @login_required
    def api_costing_close_post():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.costing_period_close import close_costing_period
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            result = close_costing_period(
                conn,
                fiscal_year=int(data.get('fiscal_year') or data.get('year') or 0),
                period=int(data.get('period') or data.get('month') or 0),
                labor_amount=(
                    float(data['labor_amount']) if data.get('labor_amount') not in (None, '') else None
                ),
                oh_fixed_amount=(
                    float(data['oh_fixed_amount']) if data.get('oh_fixed_amount') not in (None, '') else None
                ),
                oh_variable_amount=(
                    float(data['oh_variable_amount'])
                    if data.get('oh_variable_amount') not in (None, '') else None
                ),
                cost_reductions=float(data.get('cost_reductions') or 0),
                close_idle_now=bool(data.get('close_idle_now', True)),
                created_by=_username(),
                auto_posted=False,
                replace_existing=bool(data.get('replace_existing') or data.get('reclose')),
                commit=True,
            )
            reclose = bool(data.get('replace_existing') or data.get('reclose'))
            return jsonify({
                'success': True,
                'data': result,
                'message': (
                    f"{'Đã chạy lại thủ công' if reclose else 'Đã chốt'} "
                    f"kỳ {result.get('period')}/{result.get('fiscal_year')}: "
                    f"GT đơn vị thực tế {float(result.get('actual_unit_cost') or 0):,.0f} ₫"
                ),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('close post: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-period-close/reverse', methods=['POST'])
    @login_required
    def api_costing_close_reverse():
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.costing_period_close import reverse_period_close
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            result = reverse_period_close(
                conn,
                fiscal_year=int(data.get('fiscal_year') or data.get('year') or 0),
                period=int(data.get('period') or data.get('month') or 0),
                created_by=_username(),
                reason=data.get('reason') or '',
                commit=True,
            )
            return jsonify({'success': True, 'data': result, 'message': result.get('message')})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('close reverse: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-period-close/reclose', methods=['POST'])
    @login_required
    def api_costing_close_reclose():
        """Đảo (nếu đã chốt) + chốt lại với 622/627 mới nhất trên sổ."""
        data = request.get_json(silent=True) or {}
        data = dict(data)
        data['replace_existing'] = True
        data['reclose'] = True
        # Reuse post handler logic inline
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.sme.costing_period_close import reclose_costing_period
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile() or {}
            ensure_sme_accounting_ready(
                conn, accounting_regime=profile.get('accounting_regime'), commit=False,
            )
            result = reclose_costing_period(
                conn,
                fiscal_year=int(data.get('fiscal_year') or data.get('year') or 0),
                period=int(data.get('period') or data.get('month') or 0),
                labor_amount=(
                    float(data['labor_amount']) if data.get('labor_amount') not in (None, '') else None
                ),
                oh_fixed_amount=(
                    float(data['oh_fixed_amount']) if data.get('oh_fixed_amount') not in (None, '') else None
                ),
                oh_variable_amount=(
                    float(data['oh_variable_amount'])
                    if data.get('oh_variable_amount') not in (None, '') else None
                ),
                cost_reductions=float(data.get('cost_reductions') or 0),
                close_idle_now=bool(data.get('close_idle_now', True)),
                created_by=_username(),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': result,
                'message': (
                    f"Đã chạy lại thủ công kỳ {result.get('period')}/{result.get('fiscal_year')}: "
                    f"GT đơn vị {float(result.get('actual_unit_cost') or 0):,.0f} ₫ "
                    f"(đã đảo lần chốt trước rồi chốt lại theo sổ hiện tại)"
                ),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('close reclose: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing-period-close')
    @login_required
    def api_costing_close_list():
        conn = get_db_connection()
        try:
            from Services.sme.costing_policy import list_period_closes
            year = request.args.get('year')
            rows = list_period_closes(conn, fiscal_year=int(year) if year else None)
            return jsonify({'success': True, 'data': rows})
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()
