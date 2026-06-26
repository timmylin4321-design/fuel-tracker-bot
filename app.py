import os
import json
import logging
import traceback
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup)
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
TW_TZ = timezone(timedelta(hours=8))
SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', '油耗記錄')

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    ['⛽ 加油記錄'],
    ['📊 油耗查詢', '📋 歷史紀錄'],
    ['❓ 說明'],
], resize_keyboard=True)

STEP_ODOMETER = 'odometer'
STEP_LITERS   = 'liters'
STEP_COST     = 'cost'

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
    def log_message(self, *args): pass
def run_health_server(port):
    HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever()

_sheet = None
_sheet_ts = 0
_SHEET_TTL = 1800

def get_sheet():
    global _sheet, _sheet_ts
    now = time.time()
    if _sheet is None or (now - _sheet_ts) > _SHEET_TTL:
        scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
        j = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(j),scope) if j else ServiceAccountCredentials.from_json_keyfile_name('credentials.json',scope)
        _sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
        _sheet_ts = now
        if _sheet.row_count==0 or _sheet.cell(1,1).value is None:
            _sheet.insert_row(['日期','里程(km)','加油(L)','金額(元)','油價(元/L)','油耗(km/L)','行駛距離(km)'],1)
    return _sheet

def get_last_odometer():
    records = get_sheet().get_all_records()
    for r in reversed(records):
        try:
            km = float(r.get('里程(km)',0))
            if km > 0: return km
        except: pass
    return None

def save_record(odometer, liters, cost, fuel_economy, distance):
    date = datetime.now(TW_TZ).strftime('%Y/%m/%d %H:%M')
    price_per_liter = round(cost/liters,1) if liters>0 else 0
    get_sheet().append_row([date,odometer,liters,cost,price_per_liter,fuel_economy,distance])
    return date

def get_stats():
    records = get_sheet().get_all_records()
    if not records: return None
    economies = [float(r['油耗(km/L)']) for r in records if r.get('油耗(km/L)') and float(r.get('油耗(km/L)',0))>0]
    total_liters = sum(float(r.get('加油(L)',0)) for r in records)
    total_cost = sum(float(r.get('金額(元)',0)) for r in records)
    if not economies: return {'count':len(records),'total_liters':round(total_liters,1),'total_cost':round(total_cost,0),'avg_economy':0,'best_economy':0,'worst_economy':0,'last_economy':0}
    return {'count':len(records),'avg_economy':round(sum(economies)/len(economies),2),'best_economy':round(max(economies),2),'worst_economy':round(min(economies),2),'total_liters':round(total_liters,1),'total_cost':round(total_cost,0),'last_economy':round(economies[-1],2)}

def get_recent_records(n=5):
    records = get_sheet().get_all_records()
    return records[-n:] if len(records)>=n else records


async def start(update, context):
    context.user_data.clear()
    await update.message.reply_text('⛽ 油耗記錄 Bot！\n點「⛽ 加油記錄」開始。', reply_markup=MAIN_KEYBOARD)

async def handle_message(update, context):
    text = update.message.text.strip()
    step = context.user_data.get('step')
    try:
        if step == STEP_ODOMETER:
            odometer = float(text.replace(',',''))
            last_km = get_last_odometer()
            if last_km and odometer <= last_km:
                await update.message.reply_text(f'⚠️ 讀數（{odometer:,.0f}）不能小於上次（{last_km:,.0f}）\n請重新輸入：'); return
            context.user_data['odometer'] = odometer
            context.user_data['step'] = STEP_LITERS
            await update.message.reply_text(f'✅ 里程：{odometer:,.0f} km\n\n請輸入加油公升數（L）：'); return
        if step == STEP_LITERS:
            liters = float(text.replace(',',''))
            if liters <= 0: await update.message.reply_text('公升數必須大於 0：'); return
            context.user_data['liters'] = liters
            context.user_data['step'] = STEP_COST
            await update.message.reply_text(f'✅ 加油：{liters} L\n\n請輸入總金額（元）：'); return
        if step == STEP_COST:
            cost = float(text.replace(',',''))
            if cost <= 0: await update.message.reply_text('金額必須大於 0：'); return
            odometer = context.user_data['odometer']
            liters = context.user_data['liters']
            last_km = get_last_odometer()
            context.user_data.clear()
            if last_km and odometer > last_km:
                distance = round(odometer - last_km, 1)
                fuel_economy = round(distance / liters, 2)
                economy_text = f'🔥 本次油耗：{fuel_economy} km/L\n📏 行駛距離：{distance:,.0f} km'
            else:
                distance = 0; fuel_economy = 0
                economy_text = '（首次記錄，下次加油後計算油耗）'
            date = save_record(odometer, liters, cost, fuel_economy, distance)
            await update.message.reply_text(
                f'✅ 記錄完成！\n📅 {date}\n🛣️ 里程：{odometer:,.0f} km\n⛽ 加油：{liters} L\n💰 金額：NT${cost:,.0f}\n💧 油價：NT${round(cost/liters,1)}/L\n{economy_text}',
                reply_markup=MAIN_KEYBOARD); return
        if text == '⛽ 加油記錄':
            last_km = get_last_odometer()
            hint = f'（上次里程：{last_km:,.0f} km）' if last_km else '（首次記錄）'
            context.user_data['step'] = STEP_ODOMETER
            await update.message.reply_text(f'請輸入目前里程表讀數 {hint}\n例如：12500'); return
        if text == '📊 油耗查詢':
            stats = get_stats()
            if not stats: await update.message.reply_text('尚無記錄', reply_markup=MAIN_KEYBOARD); return
            msg = (f'📊 油耗統計（共 {stats["count"]} 次）\n\n🔥 最近一次：{stats["last_economy"]} km/L\n📈 平均油耗：{stats["avg_economy"]} km/L\n🏆 最佳油耗：{stats["best_economy"]} km/L\n📉 最差油耗：{stats["worst_economy"]} km/L\n\n⛽ 累計加油：{stats["total_liters"]} L\n💰 累計花費：NT${stats["total_cost"]:,.0f}')
            await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD); return
        if text == '📋 歷史紀錄':
            records = get_recent_records(5)
            if not records: await update.message.reply_text('尚無記錄', reply_markup=MAIN_KEYBOARD); return
            lines = ['📋 最近 5 次記錄：\n']
            for r in reversed(records):
                economy = r.get('油耗(km/L)','-')
                lines.append(f"📅 {str(r.get('日期',''))[:10]}\n  ⛽ {r.get('加油(L)',0)}L  💰 NT${float(r.get('金額(元)',0)):,.0f}  🔥 {economy} km/L")
            await update.message.reply_text('\n'.join(lines), reply_markup=MAIN_KEYBOARD); return
        if text in ['❓ 說明','說明']:
            await update.message.reply_text('⛽ 加油記錄：里程→公升→金額\n📊 油耗查詢：統計資料\n📋 歷史紀錄：最近 5 次', reply_markup=MAIN_KEYBOARD); return
        if text in ['取消'] and step:
            context.user_data.clear(); await update.message.reply_text('已取消', reply_markup=MAIN_KEYBOARD); return
        await update.message.reply_text('請點選下方按鈕', reply_markup=MAIN_KEYBOARD)
    except ValueError:
        await update.message.reply_text('請輸入數字')
    except Exception as e:
        logger.error(e); traceback.print_exc()
        await update.message.reply_text(f'錯誤：{str(e)[:100]}', reply_markup=MAIN_KEYBOARD)

def main():
    import asyncio; asyncio.set_event_loop(asyncio.new_event_loop())
    port = int(os.environ.get('PORT',8080))
    threading.Thread(target=run_health_server, args=(port,), daemon=True).start()
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__': main()
