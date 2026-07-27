from services.invoice_providers import get_provider

# Config demo từ PDF
config = {
    "provider": "matbao",
    "api_url": "https://demo-api-hddt.matbao.in:11443",
    "tax_code": "0302712571-999",
    "username": "admin",
    "password": "Gtybf@12sd",
    # Các field khác nếu cần
}

# Data hóa đơn test từ PDF page 3-4
invoice_data = [
    {
        "KHMSHDon": "1",
        "KHHDon": "C25TAT",
        "NLap": "2025-03-07T08:00:00",
        "DVTTe": "VND",
        "TGia": 1,
        "HTTToan": "TienMat",
        "MaTraCuu": "TEST_TRA_CUU",
        "MTChieu": "TEST_THAM_CHIEU",
        "GChu": "Ghi chú test",
        "TCHDon": 0,
        "MSHDonDCLQuan": "",
        "KHMSHDCLQuan": "",
        "KHHDCLQuan": "",
        "SHDCLQuan": "",
        "NLHDCLQuan": "2025-03-07T08:00:00",
        "NMua_Ten": "Khách hàng test",
        "NMua_MST": "0100101010",
        "NMua_DChi": "Địa chỉ test",
        "NMua_MKHang": "MKH test",
        "NMua_SDThoai": "0123456789",
        "NMua_DCTDTu": "email@test.com",
        "NMua_HVTNMHang": "Họ tên mua hàng",
        "NMua_STKNHang": "STK ngân hàng",
        "NMua_TNHang": "Tên ngân hàng",
        "NBan_MCHang": "Cửa hàng 1",
        "NBan_TCHang": "Tên cửa hàng",
        "NMua_CCCDan": "CCCD test",
        "NMua_MDVQHNSach": "MDV test",
        "NMua_SHChieu": "Hộ chiếu test",
        "DSHHDVu": [
            {
                "TChat": 1,
                "STT": 1,
                "MHHDVu": "MH001",
                "THHDVu": "Sản phẩm test",
                "DVTinh": "Cái",
                "SLuong": 2,
                "DGia": 50000,
                "ThTienChuaCK": 100000,
                "TLCKhau": 0,
                "STCKhau": 0,
                "ThTien": 100000,
                "TSuat": 10,
                "TThue": 10000,
                "TgTien": 110000,
                "MoRong1": "MR1",
                # ... MoRong2-10 if needed
            }
        ],
        "DSLPhi": [],
        "TgThTien": 100000,
        "TgTThue": 10000,
        "TTCKTMai": 0,
        "TGTKhac": 0,
        "TgTTTBSo": 110000,
        "TgTTTBChu": "Một trăm mười nghìn đồng",
        "MoRong1": "MR1",
        # ... MoRong2-10
    }
]

# Test
provider = get_provider("matbao", config)
result = provider.create_invoice(invoice_data)
print(json.dumps(result, indent=4))