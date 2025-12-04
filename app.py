import streamlit as st
import datetime
import pandas as pd
import base64
import io
import re

import dropbox
from dropbox.files import WriteMode
import json

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound, APIError

# =========================================================
# 基本設定
# =========================================================
st.set_page_config(page_title="營養師平台專案 PM 系統（Google Sheet 版）", layout="wide")

# =========================================================
# Google Sheet 設定
# =========================================================
SPREADSHEET_ID = st.secrets["project"]["sheet_id"]

MEETINGS_HEADERS = ["id", "date", "title", "raw_requirement"]
SRS_INDEX_HEADERS = [
    "id",
    "meeting_id",
    "title",
    "desc",
    "problem",
    "goal",
    "ui_location",
    "ui_image_name",
    "version",
    "change_note",
    "created_at",
    "status",
    "review_comment",
]

# 工程師任務欄位（固定格式，result_url 最後會存 JSON）
TASK_HEADERS = [
    "id",
    "name",
    "description",
    "engineer",
    "estimated_hours",
    "start_date",
    "end_date",
    "engineer_understand_status",
    "done_status",
    "client_status",
    "result_url",
]

SRS_OVERVIEW_HEADERS = ["欄位名稱", "值"]

# =========================================================
# Google Sheet Client
# =========================================================
@st.cache_resource
def get_gsheet_client():
    creds_info = st.secrets["gcp_service_account"]
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc


def get_main_sh():
    gc = get_gsheet_client()
    return gc.open_by_key(SPREADSHEET_ID)

# =========================================================
# Dropbox 連線
# =========================================================
DBX = dropbox.Dropbox(st.secrets["dropbox"]["token"])

def upload_to_dropbox(path_in_dropbox: str, file_bytes: bytes) -> str:
    """上傳檔案至 Dropbox 並回傳 raw URL"""
    try:
        DBX.files_upload(file_bytes, path_in_dropbox, mode=WriteMode("overwrite"))
    except Exception as e:
        st.error(f"Dropbox 上傳錯誤: {e}")
        raise e

    # 建立/取得連結
    try:
        link = DBX.sharing_create_shared_link_with_settings(path_in_dropbox)
        url = link.url
    except:
        existing = DBX.sharing_list_shared_links(path=path_in_dropbox).links
        url = existing[0].url if existing else None

    if not url:
        raise Exception("Dropbox 無法建立下載連結")

    # 統一轉 raw
    url = url.replace("?dl=0", "?raw=1").replace("?dl=1", "?raw=1")
    if "raw=1" not in url:
        url += "?raw=1"
    return url

# =========================================================
# 工具：自動 retry（Google API 429 時）
# =========================================================
def with_retry(func, *args, **kwargs):
    import time
    for i in range(5):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "Quota exceeded" in str(e):
                time.sleep(1.2 + i)
            else:
                raise
    raise Exception("Google API 連線多次失敗，請稍後再試。")


# =========================================================
# 工具：確保工作表存在
# =========================================================
def ensure_worksheet(sh, title: str, headers=None):
    try:
        ws = with_retry(sh.worksheet, title)
        if headers:
            existing = ws.row_values(1)
            if not existing:
                with_retry(ws.update, "A1", [headers])
            return ws
        return ws
    except WorksheetNotFound:
        ws = with_retry(sh.add_worksheet, title=title, rows="1000", cols="30")
        if headers:
            with_retry(ws.update, "A1", [headers])
        return ws


def read_all(ws):
    return with_retry(ws.get_all_records)


def rewrite_sheet(ws, headers, rows):
    with_retry(ws.clear)
    with_retry(ws.update, "A1", [headers])
    if rows:
        with_retry(ws.update, "A2", rows)


def safe_filename(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", text)
    return text[:40]


# =========================================================
# Session 初始化
# =========================================================
def init_session():
    ss = st.session_state
    if "role" not in ss:
        ss.role = None
    if "submissions" not in ss:
        ss.submissions = {}


def role_label_zh(role: str) -> str:
    return {
        "pm": "專案管理者（PM）",
        "engineer": "工程師（Engineer）",
        "client": "業主（Client）",
    }.get(role, "未登入")


# =========================================================
# 📌 會議 + SRS 建立（PM）
# =========================================================
def page_pm_meeting_srs():
    if st.session_state.role != "pm":
        st.warning("此區僅 PM 可使用。")
        return

    st.header("1️⃣ 會議與 SRS 建立")

    sh = get_main_sh()
    ws_meetings = ensure_worksheet(sh, "meetings", MEETINGS_HEADERS)
    ws_srs = ensure_worksheet(sh, "srs_index", SRS_INDEX_HEADERS)

    # ===========================
    # ✏ 建立 Meeting
    # ===========================
    st.subheader("✏ 建立 Meeting")

    with st.form("meeting_form"):
        date = st.date_input("📅 日期", datetime.date.today())
        title = st.text_input("📌 會議主題（必填）")
        raw_req = st.text_area("📝 業主需求（寫入 SRS Problem）")

        ok = st.form_submit_button("建立 Meeting")

        if ok:
            if not title.strip():
                st.error("會議主題為必填")
            else:
                existing = read_all(ws_meetings)
                new_id = (max([int(m["id"]) for m in existing]) + 1) if existing else 1

                new_row = [new_id, date.isoformat(), title, raw_req]

                rows = [
                    [m["id"], m["date"], m["title"], m["raw_requirement"]]
                    for m in existing
                ]
                rows.append(new_row)

                rewrite_sheet(ws_meetings, MEETINGS_HEADERS, rows)
                st.success(f"已建立 Meeting：M-{new_id}")

    st.markdown("---")

    # ===========================
    # 📘 建立 SRS
    # ===========================
    st.subheader("📘 從 Meeting 建立 SRS")

    meetings = read_all(ws_meetings)
    if not meetings:
        st.info("尚無 Meeting，請先建立。")
        return

    meeting_map = {
        f"M-{m['id']} | {m['date']} | {m['title']}": m for m in meetings
    }

    meeting_key = st.selectbox("選擇會議來源", list(meeting_map.keys()))
    sel_meeting = meeting_map[meeting_key]

    st.caption("Problem 自動帶入為 Meeting 的需求描述。")

    with st.form("srs_form"):
        title = st.text_input("📘 功能名稱（必填）")
        desc = st.text_area("📖 功能描述")

        st.text_area(
            "❗ Problem（自動帶入）",
            value=sel_meeting["raw_requirement"],
            disabled=True,
        )

        goal = st.text_area("🎯 Goal（PM 萃取目標）")
        ui_loc = st.text_input("📍 UI 位置描述")

        # ========= UI 圖片上傳 =========
        ui_img_file = st.file_uploader(
            "🖼 上傳 UI 圖片（可選）", type=["png", "jpg", "jpeg", "webp"]
        )
        ui_img_url = ""

        if ui_img_file:
            file_bytes = ui_img_file.getvalue()
            folder = f"/pm_system/srs_ui/"
            filename = f"{int(datetime.datetime.now().timestamp())}_{ui_img_file.name}"
            ui_img_url = upload_to_dropbox(folder + filename, file_bytes)

        # 基本欄位
        version = st.text_input("版本", "v0.1")
        change_note = st.text_area("版本變更說明")

        ok2 = st.form_submit_button("建立 SRS")

        if ok2:
            if not title.strip():
                st.error("SRS 功能名稱為必填")
            else:
                exist = read_all(ws_srs)
                new_id = (max([int(s["id"]) for s in exist]) + 1) if exist else 1
                created = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                problem = sel_meeting["raw_requirement"]

                # 寫入 srs_index 的 row
                new_row = [
                    new_id,
                    int(sel_meeting["id"]),
                    title,
                    desc,
                    problem,
                    goal,
                    ui_loc,
                    ui_img_url,  # ← Dropbox 圖片連結
                    version,
                    change_note,
                    created,
                    "待確認",
                    "",
                ]

                # 合併舊資料
                rows = []
                for s in exist:
                    rows.append([
                        s["id"], s["meeting_id"], s["title"], s["desc"],
                        s["problem"], s["goal"], s["ui_location"], s["ui_image_name"],
                        s["version"], s["change_note"], s["created_at"],
                        s.get("status", "待確認"), s.get("review_comment", "")
                    ])
                rows.append(new_row)

                rewrite_sheet(ws_srs, SRS_INDEX_HEADERS, rows)

                # 建立 SRS 四張子表
                srs_obj = {
                    "id": new_id,
                    "meeting_id": int(sel_meeting["id"]),
                    "title": title,
                    "desc": desc,
                    "problem": problem,
                    "goal": goal,
                    "ui_location": ui_loc,
                    "ui_image_name": ui_img_url,
                    "version": version,
                    "change_note": change_note,
                    "created_at": created,
                }

                create_srs_worksheets(sh, srs_obj, sel_meeting)

                st.success(f"SRS-{new_id} 建立成功！")

def create_srs_worksheets(sh, srs, meeting):
    srs_id = srs["id"]

    # ---------------------------
    # 1. SRS_OVERVIEW
    # ---------------------------
    ws_over = ensure_worksheet(sh, f"SRS_OVERVIEW_{srs_id}", SRS_OVERVIEW_HEADERS)

    rows = []
    def add(k, v): rows.append([k, v])

    add("SRS ID", srs_id)
    add("來源會議", f"M-{meeting['id']} | {meeting['date']} | {meeting['title']}")
    add("功能名稱", srs["title"])
    add("功能描述", srs["desc"])
    add("Problem", srs["problem"])
    add("Goal", srs["goal"])
    add("UI 位置描述", srs["ui_location"])
    add("UI 圖片檔名", srs["ui_image_name"])
    add("版本", srs["version"])
    add("變更說明", srs["change_note"])
    add("建立時間", srs["created_at"])
    add("審核狀態", "待確認")
    add("審核意見（業主）", "")

    rewrite_sheet(ws_over, SRS_OVERVIEW_HEADERS, rows)

    # ---------------------------
    # 2. SRS_CONTENT
    # ---------------------------
    ws_ct = ensure_worksheet(sh, f"SRS_CONTENT_{srs_id}", ["欄位", "值"])
    rewrite_sheet(ws_ct, ["欄位", "值"], [
        ["SRS ID", srs_id],
        ["功能名稱", srs["title"]],
        ["功能描述", srs["desc"]],
        ["Problem", srs["problem"]],
        ["Goal", srs["goal"]],
        ["UI 位置描述", srs["ui_location"]],
        ["UI 圖片網址", srs["ui_image_name"]],
        ["版本", srs["version"]],
        ["變更說明", srs["change_note"]],
        ["建立時間", srs["created_at"]],
    ])

    # ---------------------------
    # 3. SRS_TASKS（工程師回報）
    # ---------------------------
    ws_tasks = ensure_worksheet(sh, f"SRS_TASKS_{srs_id}", TASK_HEADERS)
    rewrite_sheet(ws_tasks, TASK_HEADERS, [])

    # ---------------------------
    # 4. PM_TASKS（自由欄位）
    # ---------------------------
    pm_title = f"PM_TASKS_{srs_id}"
    try:
        sh.worksheet(pm_title)
    except:
        sh.add_worksheet(title=pm_title, rows="200", cols="20")

# =========================================================
# 🛠 PART 3 — 工程師任務工作台（含 Dropbox 成果檔案上傳）
# =========================================================
def page_engineer():
    if st.session_state.role != "engineer":
        st.warning("此區僅工程師（Engineer）可使用。")
        return

    st.header("🛠 工程師任務工作台")

    sh = get_main_sh()
    ws_srs = ensure_worksheet(sh, "srs_index", SRS_INDEX_HEADERS)
    srs_list = read_all(ws_srs)

    if not srs_list:
        st.info("目前尚無 SRS。")
        return

    # ===============================================
    # 1. 工程師選擇 SRS
    # ===============================================
    options = {f"SRS-{s['id']} | {s['title']}": int(s["id"]) for s in srs_list}
    label = st.selectbox("請選擇 SRS", list(options.keys()))
    srs_id = options[label]

    st.markdown("---")

    # ===============================================
    # 2. 顯示 SRS Overview
    # ===============================================
    st.subheader("📘 SRS 詳細內容")

    try:
        ws_overview = sh.worksheet(f"SRS_OVERVIEW_{srs_id}")
        overview_values = read_all(ws_overview)
        df_over = pd.DataFrame(overview_values)
        st.dataframe(df_over, use_container_width=True)
    except:
        st.info("此 SRS 尚無 Overview 資料")

    st.markdown("---")

    # ===============================================
    # 3. 顯示 PM 子任務
    # ===============================================
    st.subheader("📌 PM 子任務列表")

    pm_sheet = f"PM_TASKS_{srs_id}"

    try:
        ws_pm = sh.worksheet(pm_sheet)
        pm_values = ws_pm.get_all_values()

        if len(pm_values) <= 1:
            st.info("此 SRS 尚無 PM 拆解子任務")
            df_pm = pd.DataFrame()
        else:
            df_pm = pd.DataFrame(pm_values[1:], columns=pm_values[0])
            st.dataframe(df_pm, use_container_width=True)

    except WorksheetNotFound:
        st.info("尚未建立 PM 子任務表")
        df_pm = pd.DataFrame()

    st.markdown("---")

    # ===============================================
    # 4. 工程師回報任務（含成果檔案）
    # ===============================================
    st.subheader("📝 工程師任務回報")

    ws_engineer = ensure_worksheet(sh, f"SRS_TASKS_{srs_id}", TASK_HEADERS)
    existing_tasks = read_all(ws_engineer)

    pm_subtasks = df_pm.iloc[:, 0].tolist() if not df_pm.empty else []

    with st.form("eng_report_form"):
        subtask_name = st.selectbox(
            "要回報的 PM 子任務",
            pm_subtasks if pm_subtasks else ["（尚無子任務，請 PM 建立）"],
        )

        understand_status = st.radio(
            "理解狀態",
            ["已理解", "需要更多資料"],
        )

        est_hours = st.number_input(
            "預估工時（小時）",
            min_value=0.0,
            step=0.5,
        )

        start_date = st.date_input("預計開始日期")
        end_date = st.date_input("預計結束日期")

        # ========= 工程師成果檔案上傳 =========
        result_files = st.file_uploader(
            "📎 上傳成果檔案（可多個）",
            type=["png", "jpg", "jpeg", "pdf", "csv", "xlsx", "zip", "txt"],
            accept_multiple_files=True
        )

        submitted = st.form_submit_button("提交回報")

    if submitted:
        new_id = (max([int(t["id"]) for t in existing_tasks]) + 1) if existing_tasks else 1

        # ======================================================
        #  🔥 Step 1：將所有成果檔案上傳 Dropbox
        # ======================================================
        upload_urls = []
        srs_folder = f"/pm_system/srs_result/SRS_{srs_id}/"

        if result_files:
            for f in result_files:
                bytes_data = f.getvalue()
                filename = f"{new_id}_{int(datetime.datetime.now().timestamp())}_{f.name}"
                drop_path = srs_folder + filename

                url = upload_to_dropbox(drop_path, bytes_data)
                upload_urls.append(url)

        result_url = "\n".join(upload_urls) if upload_urls else ""

        # ======================================================
        #  🔥 Step 2：寫入 Google Sheet
        # ======================================================
        new_row = [
            new_id,
            subtask_name,
            "",                        # description 不使用
            "Engineer",
            est_hours,
            start_date.isoformat(),
            end_date.isoformat(),
            understand_status,
            "進行中" if understand_status == "已理解" else "等待資料",
            "待確認",                 # client_status
            result_url,               # Dropbox URLs
        ]

        rows = []
        for t in existing_tasks:
            rows.append([
                t["id"], t["name"], t["description"], t["engineer"],
                t["estimated_hours"], t["start_date"], t["end_date"],
                t["engineer_understand_status"], t["done_status"],
                t["client_status"], t["result_url"]
            ])
        rows.append(new_row)

        rewrite_sheet(ws_engineer, TASK_HEADERS, rows)
        st.success("已成功回報！Dashboard 已同步更新。")

    st.markdown("---")

    # ===============================================
    # 5. 顯示工程師自己的所有回報
    # ===============================================
    st.subheader("📦 此 SRS 的所有工程師回報")

    updated = read_all(ws_engineer)

    if updated:
        df_show = pd.DataFrame(updated)
        st.dataframe(df_show, use_container_width=True)

        # 如果有成果連結 → 直接能預覽圖片
        for row in updated:
            if row.get("result_url"):
                st.write(f"### 📄 任務 {row['id']} 成果連結")
                links = row["result_url"].split("\n")

                for link in links:
                    if any(link.lower().endswith(ext) for ext in ["png","jpg","jpeg","webp"]):
                        st.image(link)
                    else:
                        st.write(f"- 🔗 {link}")

    else:
        st.info("此 SRS 尚無工程師回報。")

# =========================================================
# 🧾 PART 4 — 業主：SRS 審核
# =========================================================
def page_client_srs_review():
    if st.session_state.role != "client":
        st.warning("此區僅業主（Client）可使用。")
        return

    st.header("🧾 SRS 審核（業主）")

    sh = get_main_sh()
    ws_srs = ensure_worksheet(sh, "srs_index", SRS_INDEX_HEADERS)
    srs_list = read_all(ws_srs)

    if not srs_list:
        st.info("目前尚無 SRS。")
        return

    # 選擇要審核的 SRS
    options = {
        f"SRS-{s['id']} | {s['title']} | 狀態：{s.get('status','待確認')}": int(s["id"])
        for s in srs_list
    }
    label = st.selectbox("選擇要審核的 SRS", list(options.keys()))
    srs_id = options[label]

    # 找出該筆 SRS
    srs = next(s for s in srs_list if int(s["id"]) == srs_id)

    # ===============================================
    # 顯示 SRS Overview
    # ===============================================
    st.subheader("📘 SRS 詳細內容")

    try:
        ws_overview = sh.worksheet(f"SRS_OVERVIEW_{srs_id}")
        overview = read_all(ws_overview)
        df_overview = pd.DataFrame(overview)
        st.dataframe(df_overview, use_container_width=True)

        # 如果有 UI image → 自動預覽
        for row in overview:
            if row["欄位名稱"] == "UI 圖片檔名" and row["值"]:
                url = row["值"]
                if any(url.lower().endswith(ext) for ext in ["jpg","jpeg","png","webp"]):
                    st.image(url, caption="UI 介面示意圖")
    except:
        st.warning("找不到此 SRS 的 Overview 表")

    st.markdown("---")

    # ===============================================
    # 業主審核操作
    # ===============================================
    st.subheader("📝 審核操作")

    new_status = st.radio(
        "審核狀態",
        ["待確認", "已通過"],
        index=0 if srs.get("status","待確認") == "待確認" else 1,
    )
    new_comment = st.text_area("審核意見", value=srs.get("review_comment",""))

    if st.button("💾 儲存審核結果"):

        # ===== 更新 srs_index =====
        updated_rows = []
        for x in read_all(ws_srs):
            if int(x["id"]) == srs_id:
                x["status"] = new_status
                x["review_comment"] = new_comment
            updated_rows.append([
                x["id"], x["meeting_id"], x["title"], x["desc"], x["problem"],
                x["goal"], x["ui_location"], x["ui_image_name"],
                x["version"], x["change_note"], x["created_at"],
                x.get("status","待確認"),
                x.get("review_comment",""),
            ])
        rewrite_sheet(ws_srs, SRS_INDEX_HEADERS, updated_rows)

        # ===== 更新 overview sheet =====
        try:
            ws_over = sh.worksheet(f"SRS_OVERVIEW_{srs_id}")
            ov = read_all(ws_over)

            ov_new_rows = []
            for row in ov:
                k = row.get("欄位名稱")
                if k == "審核狀態":
                    ov_new_rows.append([k, new_status])
                elif k == "審核意見（業主）":
                    ov_new_rows.append([k, new_comment])
                else:
                    ov_new_rows.append([k, row.get("值")])

            rewrite_sheet(ws_over, SRS_OVERVIEW_HEADERS, ov_new_rows)
        except:
            pass

        st.success("SRS 審核結果已更新！Dashboard 已同步。")

# =========================================================
# 📦 PART 4 — 業主：工程師任務成果審核（含 Dropbox 預覽）
# =========================================================
def page_client_task_review():
    if st.session_state.role != "client":
        st.warning("此區僅業主（Client）可使用。")
        return

    st.header("📦 任務成果審核（業主）")

    sh = get_main_sh()
    ws_srs = ensure_worksheet(sh, "srs_index", SRS_INDEX_HEADERS)
    srs_list = read_all(ws_srs)

    if not srs_list:
        st.info("目前尚無 SRS。")
        return

    # 選擇 SRS
    options = {f"SRS-{s['id']} | {s['title']}": int(s["id"]) for s in srs_list}
    label = st.selectbox("選擇 SRS 任務表", list(options.keys()))
    srs_id = options[label]

    ws_tasks_name = f"SRS_TASKS_{srs_id}"

    try:
        ws_tasks = sh.worksheet(ws_tasks_name)
        tasks = read_all(ws_tasks)
    except WorksheetNotFound:
        st.info("此 SRS 尚無工程師任務")
        return

    # ===============================================
    # 清單檢視
    # ===============================================
    st.subheader("📘 工程師所有回報紀錄")
    st.dataframe(pd.DataFrame(tasks), use_container_width=True)
    st.markdown("---")

    # ===============================================
    # 審核區塊（每筆展開）
    # ===============================================
    st.subheader("📝 審核任務成果")

    updated_rows = []

    for t in tasks:
        tid = int(t["id"])

        with st.expander(f"任務 {tid}：{t.get('name','(未命名)')}"):

            st.write(f"負責工程師：{t.get('engineer','')}")
            st.write(f"理解狀態：{t.get('engineer_understand_status','')}")
            st.write(f"任務狀態：{t.get('done_status','')}")
            st.write(f"預估工時：{t.get('estimated_hours','')}")
            st.write(f"期間：{t.get('start_date','')} → {t.get('end_date','')}")

            # ========= 成果連結預覽 =========
            urls = t.get("result_url","")
            if urls:
                st.write("📎 成果檔案：")
                url_list = urls.split("\n")

                for u in url_list:
                    if any(u.lower().endswith(ext) for ext in ["png","jpg","jpeg","webp"]):
                        st.image(u, caption="成果圖片預覽")
                    else:
                        st.write(f"- 🔗 {u}")

            # ========= 審核選項 =========
            client_status = st.selectbox(
                "審核狀態",
                ["待確認", "已通過"],
                index = 0 if t.get("client_status","待確認") == "待確認" else 1,
                key=f"client_status_{tid}_{srs_id}"
            )

            updated_rows.append([
                tid,
                t.get("name",""),
                t.get("description",""),
                t.get("engineer",""),
                t.get("estimated_hours",""),
                t.get("start_date",""),
                t.get("end_date",""),
                t.get("engineer_understand_status",""),
                t.get("done_status",""),
                client_status,                # 更新審核狀態
                t.get("result_url",""),
            ])

    if st.button("💾 儲存所有審核結果"):
        rewrite_sheet(ws_tasks, TASK_HEADERS, updated_rows)
        st.success("任務成果審核成功，Dashboard 已同步更新！")
        
# =========================================================
# Dashboard（所有角色都可看）
# =========================================================
def page_dashboard():
    st.header("📊 專案 Dashboard（全專案總覽）")

    sh = get_main_sh()

    # 讀取 SRS 與 Meeting
    ws_srs = ensure_worksheet(sh, "srs_index", SRS_INDEX_HEADERS)
    ws_meetings = ensure_worksheet(sh, "meetings", MEETINGS_HEADERS)

    srs_list = read_all(ws_srs)
    meetings = read_all(ws_meetings)
    meeting_map = {int(m["id"]): m for m in meetings}

    # ============================================================
    # 📘 SRS 進度總覽
    # ============================================================
    st.subheader("📘 SRS 進度總覽")

    srs_overall_rows = []

    for s in srs_list:
        srs_id = int(s["id"])

        # PM 子任務（自由欄位）
        try:
            ws_pm = sh.worksheet(f"PM_TASKS_{srs_id}")
            pm_values = ws_pm.get_all_values()
            pm_task_count = len(pm_values) - 1 if len(pm_values) > 1 else 0
        except:
            pm_task_count = 0

        # 工程師任務
        try:
            ws_tasks = sh.worksheet(f"SRS_TASKS_{srs_id}")
            eng_tasks = read_all(ws_tasks)
        except:
            eng_tasks = []

        total = len(eng_tasks)
        done = len([t for t in eng_tasks if t.get("done_status") == "已完成"])
        progress_rate = f"{done}/{total}" if total else "0/0"

        mid = s.get("meeting_id")
        m = meeting_map.get(int(mid))
        meeting_str = f"M-{m['id']} | {m['date']} | {m['title']}" if m else ""

        srs_overall_rows.append({
            "SRS ID": srs_id,
            "功能名稱": s["title"],
            "版本": s["version"],
            "來源會議": meeting_str,
            "PM 子任務數": pm_task_count,
            "工程師任務進度": progress_rate,
            "SRS 狀態": s.get("status", "待確認"),
            "業主審核意見": s.get("review_comment", "")
        })

    st.dataframe(pd.DataFrame(srs_overall_rows), use_container_width=True)

    st.markdown("---")

    # ============================================================
    # 🟧 PM 子任務總覽
    # ============================================================
    st.subheader("📌 PM 子任務總覽（按 SRS 分組）")

    for s in srs_list:
        srs_id = int(s["id"])
        st.markdown(f"### 🔹 SRS-{srs_id}：{s['title']}")

        try:
            ws_pm = sh.worksheet(f"PM_TASKS_{srs_id}")
            values = ws_pm.get_all_values()

            if len(values) <= 1:
                st.info("尚無 PM 子任務")
                continue

            df_pm = pd.DataFrame(values[1:], columns=values[0])
            st.dataframe(df_pm, use_container_width=True)

        except:
            st.info("尚無 PM 子任務表")

    st.markdown("---")

    # ============================================================
    # 🛠 工程師任務總覽
    # ============================================================
    st.subheader("🛠 工程師任務列表（含逾期判斷）")

    all_eng_rows = []

    for s in srs_list:
        srs_id = int(s["id"])

        try:
            ws_tasks = sh.worksheet(f"SRS_TASKS_{srs_id}")
            tasks = read_all(ws_tasks)
        except:
            continue

        for t in tasks:
            overdue = ""
            end = t.get("end_date")
            done_status = t.get("done_status", "")

            try:
                if end and done_status != "已完成":
                    if datetime.date.fromisoformat(end) < datetime.date.today():
                        overdue = "⚠ 逾期"
            except:
                pass

            all_eng_rows.append({
                "SRS ID": srs_id,
                "任務名稱": t.get("name", ""),
                "負責工程師": t.get("engineer", ""),
                "理解狀態": t.get("engineer_understand_status", ""),
                "預估工時": t.get("estimated_hours", ""),
                "開始日期": t.get("start_date", ""),
                "結束日期": end,
                "任務狀態": done_status,
                "業主審核": t.get("client_status", ""),
                "逾期": overdue,
            })

    if all_eng_rows:
        st.dataframe(pd.DataFrame(all_eng_rows), use_container_width=True)
    else:
        st.info("目前尚無工程師回報任務。")


# =========================================================
# PART 5 — 主入口（Routing）
# =========================================================
def main():
    init_session()

    # Sidebar 導航
    st.sidebar.title("📌 系統導航")

    # =====================================================
    # 登入 / 登出
    # =====================================================
    if st.session_state.role:
        st.sidebar.write(f"👤 目前身分：**{role_label_zh(st.session_state.role)}**")

        if st.sidebar.button("🚪 登出"):
            st.session_state.role = None
            st.rerun()

    else:
        st.sidebar.info("尚未登入，請先選擇角色。")
        return login_page()

    # =====================================================
    # 功能模組（依角色顯示）
    # =====================================================
    if st.session_state.role == "pm":
        page_name = st.sidebar.selectbox(
            "功能模組",
            ["Dashboard", "會議與 SRS 設定"],
        )

    elif st.session_state.role == "engineer":
        page_name = st.sidebar.selectbox(
            "功能模組",
            ["Dashboard", "工程師任務工作台"],
        )

    else:  # client
        page_name = st.sidebar.selectbox(
            "功能模組",
            ["Dashboard", "SRS 審核（業主）", "任務成果審核（業主）"],
        )

    # =====================================================
    # Routing（所有函式名稱已精準對應）
    # =====================================================
    if page_name == "Dashboard":
        page_dashboard()

    elif page_name == "會議與 SRS 設定":
        page_pm_meeting_srs()

    elif page_name == "工程師任務工作台":
        page_engineer()

    elif page_name == "SRS 審核（業主）":
        page_client_srs_review()

    elif page_name == "任務成果審核（業主）":
        page_client_task_review()


# =========================================================
# PART 5 — 登入畫面（選角色）
# =========================================================
def login_page():
    st.markdown(
        """
        <div style="text-align:center; padding:40px 0;">
            <h1>營養師平台 PM 系統</h1>
            <p style="color:#666;">請選擇你的角色登入</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("🧭 專案管理者（PM）", use_container_width=True, type="primary"):
            st.session_state.role = "pm"
            st.rerun()

    with col2:
        if st.button("🛠 工程師（Engineer）", use_container_width=True):
            st.session_state.role = "engineer"
            st.rerun()

    with col3:
        if st.button("🏢 業主（Client）", use_container_width=True):
            st.session_state.role = "client"
            st.rerun()


# =========================================================
# App 啟動點
# =========================================================
if __name__ == "__main__":
    main()
