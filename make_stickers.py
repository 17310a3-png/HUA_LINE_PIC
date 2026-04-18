"""
LINE 貼圖自動化製作腳本

使用方式：
  1. 手動模式：在下方 CONFIG 設定參數後，直接執行
     python make_stickers.py

  2. CLI 模式（供 N8N / webhook 自動呼叫）：
     python make_stickers.py --name otter_office --total 40 \
       --sheets "sheet1.png,sheet2.png,sheet3.png,sheet4.png,sheet5.png,sheet6.png" \
       --main 5,0 --tab 5,1

原稿格式：3欄 × 3列 = 9格（右下角 index 8 為 AI logo，自動跳過）
每張原稿有效格數：8格
40張貼圖分配（5張原稿）：
  Sheet1: 01-08
  Sheet2: 09-16
  Sheet3: 17-24
  Sheet4: 25-32
  Sheet5: 33-40
  Sheet6: main + tab（用 MAIN_SRC / TAB_SRC 指定格子）
"""

from PIL import Image, ImageFilter
import numpy as np
import os, zipfile, argparse, sys

# ============================================================
# CONFIG（手動模式用，CLI 參數會覆蓋這些值）
# ============================================================

# 套組名稱（輸出資料夾名稱 / ZIP 名稱）
SET_NAME = "cone_office"

# 原稿檔名（放在 gemini/ 資料夾）
# 3欄×3列格式，每張 8 格可用（右下角 index 8 = AI logo 自動跳過）
SHEET1 = "new角錐上班族1-1.png"   # 對應貼圖 01-08
SHEET2 = "new角錐上班族1-2.png"   # 對應貼圖 09-16
SHEET3 = "new角錐上班族2-1.png"   # 對應貼圖 17-24
SHEET4 = "new角錐上班族2-2.png"   # 對應貼圖 25-32
SHEET5 = "new角錐上班族3-1.png"   # 對應貼圖 33-40
SHEET6 = "new角錐上班族3-2.png"   # main + tab 來源

# 總貼圖數量（8 / 16 / 24 / 32 / 40）
TOTAL = 40

# main / tab 來源：(原稿編號 0=SHEET1~5=SHEET6, 格子 index)
# SHEET6 格子：0=main, 1=tab（依實際排版調整）
MAIN_SRC = (5, 0)
TAB_SRC  = (5, 1)

# 原稿格式
COLS = 3
ROWS = 3
SKIP_CELLS = {8}  # 右下角 AI logo 位置，自動跳過

# 裁切設定（通常不需要改）
STICKER_SIZE  = (320, 320)
STICKER_INNER = (310, 310)
MAIN_SIZE  = (240, 240)
MAIN_INNER = (224, 224)
TAB_SIZE   = (96, 74)
TAB_INNER  = (88, 66)

# ============================================================
# CLI 參數解析
# ============================================================

def parse_args():
    """解析 CLI 參數，若無參數則使用上方 CONFIG 預設值"""
    parser = argparse.ArgumentParser(description="LINE 貼圖自動化裁切腳本")
    parser.add_argument("--name", type=str, help="套組名稱（slug）")
    parser.add_argument("--total", type=int, help="貼圖總張數（8/16/24/32/40）")
    parser.add_argument("--sheets", type=str,
                        help="原稿檔名，逗號分隔（如 sheet1.png,sheet2.png,...）")
    parser.add_argument("--main", type=str,
                        help="main.png 來源，格式：sheet_index,cell_index（如 5,0）")
    parser.add_argument("--tab", type=str,
                        help="tab.png 來源，格式：sheet_index,cell_index（如 5,1）")
    return parser.parse_args()

def apply_cli_args(args):
    """將 CLI 參數套用到全域 CONFIG"""
    global SET_NAME, TOTAL, MAIN_SRC, TAB_SRC
    global SHEET1, SHEET2, SHEET3, SHEET4, SHEET5, SHEET6

    if args.name:
        SET_NAME = args.name
    if args.total:
        TOTAL = args.total
    if args.sheets:
        sheet_list = [s.strip() for s in args.sheets.split(",")]
        # 填入 SHEET1~SHEET6，不足的留空字串
        sheets = sheet_list + [""] * (6 - len(sheet_list))
        SHEET1, SHEET2, SHEET3, SHEET4, SHEET5, SHEET6 = sheets[:6]
    if args.main:
        parts = args.main.split(",")
        MAIN_SRC = (int(parts[0]), int(parts[1]))
    if args.tab:
        parts = args.tab.split(",")
        TAB_SRC = (int(parts[0]), int(parts[1]))

# ============================================================
# 以下不需要修改
# ============================================================

BASE       = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
GEMINI_DIR = os.path.join(BASE, "gemini")

def load_sheets():
    sheets = []
    for fname in [SHEET1, SHEET2, SHEET3, SHEET4, SHEET5, SHEET6]:
        if fname:
            path = os.path.join(GEMINI_DIR, fname)
            if os.path.exists(path):
                sheets.append(Image.open(path).convert("RGBA"))
            else:
                sheets.append(None)
    return sheets

def remove_green(cell):
    """
    綠幕去背（Chroma Key）：
    - 只刪除綠色背景（g 明顯大於 r 和 b）
    - 保留所有白色像素（文字白底、角色白描邊）
    需要生圖時背景指定為純綠色 #00FF00
    """
    cell = cell.convert("RGBA")
    data = np.array(cell, dtype=np.uint8)

    r = data[:,:,0].astype(int)
    g = data[:,:,1].astype(int)
    b = data[:,:,2].astype(int)

    green_mask = (g > 80) & (g > r + 30) & (g > b + 30) & ~((r > 200) & (b > 200))
    data[green_mask, 3] = 0

    result = Image.fromarray(data)
    alpha_img = result.split()[3]
    alpha_img = alpha_img.filter(ImageFilter.GaussianBlur(0.5))
    result.putalpha(alpha_img)

    return result


def trim_transparent(img):
    """裁掉完全透明的外圍，把角色置中的 bounding box 保留下來"""
    arr = np.array(img)
    alpha = arr[:, :, 3]
    ys, xs = np.where(alpha > 0)
    if len(ys) == 0:
        return img
    y0, y1 = ys.min(), ys.max() + 1
    x0, x1 = xs.min(), xs.max() + 1
    return img.crop((x0, y0, x1, y1))

def crop_cell(img, idx):
    """裁切指定 index 的格子（3欄×3列，跳過右下角 AI logo）"""
    W, H = img.size
    cw = W // COLS
    ch = H // ROWS
    row, col = divmod(idx, COLS)

    inset = 6  # 四邊均勻內縮，避開格線滲入（新 prompt 已無格線，小數值即可）

    x0 = col * cw + inset
    y0 = row * ch + inset
    x1 = (col + 1) * cw - inset
    y1 = (row + 1) * ch - inset
    return img.crop((x0, y0, x1, y1))

def make_canvas(cell, size, inner):
    cell = remove_green(cell)
    cell = trim_transparent(cell)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    ratio = min(inner[0] / cell.width, inner[1] / cell.height)
    new_w = int(cell.width * ratio)
    new_h = int(cell.height * ratio)
    cell = cell.resize((new_w, new_h), Image.LANCZOS)
    ox = (size[0] - new_w) // 2
    oy = (size[1] - new_h) // 2
    canvas.paste(cell, (ox, oy), cell)
    return canvas

def main():
    # 套用 CLI 參數（若有）
    args = parse_args()
    apply_cli_args(args)

    # 設定輸出路徑（在 CLI 參數套用後才能確定）
    OUT_DIR  = os.path.join(BASE, "完成圖檔區", SET_NAME)
    ZIP_PATH = os.path.join(BASE, "zip儲存區", f"{SET_NAME}.zip")
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(ZIP_PATH), exist_ok=True)

    print(f"=== 開始製作：{SET_NAME} ===")
    sheets = load_sheets()

    # 每張原稿可用格子（跳過 SKIP_CELLS）
    usable = [i for i in range(COLS * ROWS) if i not in SKIP_CELLS]

    # 分配貼圖編號到 (sheet index, cell index)
    assignments = []
    num = 1
    for si in range(len(sheets)):
        for ci in usable:
            if num > TOTAL:
                break
            if sheets[si] is None:
                print(f"  WARNING: SHEET{si+1} 不存在，跳過 {num:02d}")
                num += 1
                continue
            assignments.append((num, si, ci))
            num += 1
        if num > TOTAL:
            break

    # 裁切貼圖
    for sticker_num, si, ci in assignments:
        cell = crop_cell(sheets[si], ci)
        img = make_canvas(cell, STICKER_SIZE, STICKER_INNER)
        out_path = os.path.join(OUT_DIR, f"{sticker_num:02d}.png")
        img.save(out_path, "PNG")
        print(f"  {sticker_num:02d}.png OK")

    # main.png
    ms, mi = MAIN_SRC
    if sheets[ms]:
        cell = crop_cell(sheets[ms], mi)
        make_canvas(cell, MAIN_SIZE, MAIN_INNER).save(
            os.path.join(OUT_DIR, "main.png"), "PNG"
        )
        print("  main.png OK")

    # tab.png
    ts, ti = TAB_SRC
    if sheets[ts]:
        cell = crop_cell(sheets[ts], ti)
        make_canvas(cell, TAB_SIZE, TAB_INNER).save(
            os.path.join(OUT_DIR, "tab.png"), "PNG"
        )
        print("  tab.png OK")

    # ZIP
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(OUT_DIR)):
            if fname.endswith(".png") and fname not in ("cover.png", "tab_icon.png"):
                zf.write(os.path.join(OUT_DIR, fname), fname)
    print(f"\n  ZIP 完成：{ZIP_PATH}")
    print(f"=== 完成！共 {TOTAL} 張 ===")

    return {"out_dir": OUT_DIR, "zip_path": ZIP_PATH, "total": TOTAL}

if __name__ == "__main__":
    main()
