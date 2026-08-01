/**
 * Chuẩn hiển thị giao diện Kế toán SME (tiếng Việt, ngày dd/mm/yyyy).
 * Giá trị API/DB giữ nguyên (InStock, Active…); chỉ đổi phần người dùng nhìn thấy.
 */
(function (global) {
  'use strict';

  var STATUS_VI = {
    InStock: 'Trong kho',
    Active: 'Đang sử dụng',
    Disposed: 'Đã thanh lý',
    posted: 'Đã ghi sổ',
    void: 'Đã hủy',
    draft: 'Nháp',
    cancelled: 'Đã hủy',
    canceled: 'Đã hủy',
    reversed: 'Đã đảo',
    active: 'Đang hiệu lực',
    inactive: 'Ngừng hiệu lực',
    accrued: 'Đã trích',
    paid: 'Đã nộp',
    confirmed: 'Đã xác nhận',
    partial: 'Giao một phần',
    completed: 'Hoàn thành',
    closed: 'Đã đóng',
    open: 'Đang mở',
    Off: 'Ngừng dùng',
    ON: 'Đang dùng',
    On: 'Đang dùng',
  };

  var BRANCH_VI = {
    HQ: 'Trụ sở chính',
    ALL: 'Tất cả chi nhánh',
  };

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  /** Chuỗi ngày → dd/mm/yyyy (chấp nhận YYYY-MM-DD, Date, timestamp). */
  function fmtDateVi(value) {
    if (value == null || value === '') return '—';
    if (value instanceof Date && !isNaN(value.getTime())) {
      return pad2(value.getDate()) + '/' + pad2(value.getMonth() + 1) + '/' + value.getFullYear();
    }
    var s = String(value).trim();
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) return m[3] + '/' + m[2] + '/' + m[1];
    m = s.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    if (m) return s;
    var d = new Date(s);
    if (!isNaN(d.getTime()) && s.length >= 8) {
      return pad2(d.getDate()) + '/' + pad2(d.getMonth() + 1) + '/' + d.getFullYear();
    }
    return s.slice(0, 10);
  }

  /** Ngày hôm nay YYYY-MM-DD (cho input type=date). */
  function todayISO() {
    var d = new Date();
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  function statusLabelVi(code) {
    if (code == null || code === '') return '—';
    var key = String(code).trim();
    return STATUS_VI[key] || key;
  }

  function branchLabelVi(code) {
    if (code == null || code === '') return BRANCH_VI.HQ;
    var key = String(code).trim().toUpperCase();
    return BRANCH_VI[key] || key;
  }

  function moneyVi(v, withSuffix) {
    var n = Number(v || 0);
    var s = Math.round(n).toLocaleString('vi-VN');
    return withSuffix === false ? s : s + ' đ';
  }

  global.SmeUiVi = {
    fmtDateVi: fmtDateVi,
    todayISO: todayISO,
    statusLabelVi: statusLabelVi,
    branchLabelVi: branchLabelVi,
    moneyVi: moneyVi,
    STATUS_VI: STATUS_VI,
  };
  // Lối tắt toàn cục cho template cũ
  global.fmtDateVi = fmtDateVi;
  global.todayISO = global.todayISO || todayISO;
  global.statusLabelVi = statusLabelVi;
  global.branchLabelVi = branchLabelVi;
  global.moneyVi = moneyVi;
})(typeof window !== 'undefined' ? window : this);
