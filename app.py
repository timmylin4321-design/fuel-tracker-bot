import os
import json
import logging
import traceback
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters
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

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
    def log_message(self, *args): pass
def run_health_server(port):
    HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever()

_sheet = None; _sheet_ts = 0; _SHEET_TTL = 1800

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
            _sheet.insert_row(['日期','里程(km)','加油(L)','金額(元)','油價(元/L)','油耗(L/100km)','行駛距離(km)'],1)
    return _sheet

def get_last_odometer():
    for r in reversed(get_sheet().get_all_records()):
        try:
            km = float(r.get('里程(km)',0))
            if km > 0: return km
        except: pass
    return None

def save_record(odometer, liters, cost, economy, distance):
    date = datetime.now(TW_TZ).strftime('%Y/%m/%d %H:%M')
    price = round(cost/liters,1) if liters>0 else 0
    get_sheet().append_row([date,odometer,liters,cost,price,economy,distance])
    return date

def get_stats():
    records = get_sheet().get_all_records()
    if not records: return None
    # L/100km 越小越省油
    eco = [float(r['油耗(L/100km)']) for r in records if r.get('油耗(L/100km)') and float(r.get('油耗(L/100km)',0))>0]
    tl = sum(float(r.get('加油(L)',0)) for r in records)
    tc = sum(float(r.get('金額(元)',0)) for r in records)
    td = sum(float(r.get('行駛距離(km)',0)) for r in records)
    if not eco: return {'count':len(records),'tl':round(tl,1),'tc':round(tc,0),'td':round(td,0),'avg':0,'best':0,'worst':0,'last':0}
    return {
        'count':len(records),'avg':round(sum(eco)/len(eco),2),
        'best':round(min(eco),2),
        'worst':round(max(eco),2),
        'tl':round(tl,1),'tc':round(tc,0),'td':round(td,0),'last':round(eco[-1],2)
    }

def get_recent(n=5):
    r = get_sheet().get_all_records()
    return r[-n:] if len(r)>=n else r


async def start(update, context):
    context.user_data.clear()
    await update.message.reply_text('⛽ 油耗記錄 Bot！\n點「⛽ 加油記錄」開始。', reply_markup=MAIN_KEYBOARD)

async def handle_message(update, context):
    text = update.message.text.strip()
    try: await _proc(update, context, text)
    except Exception as e:
        logger.error(e); traceback.print_exc()
        await update.message.reply_text(f'錯誤：{str(e)[:100]}', reply_markup=MAIN_KEYBOARD)

async def _proc(update, context, text):
    if context.user_data.get('waiting_fuel'):
        context.user_data.pop('waiting_fuel')
        parts = text.replace('/', ' ').replace(',', '').split()
        if len(parts) < 3:
            await update.message.reply_text('格式錯誤！請輸入三個數字：\n里程 公升 金額\n例如：12500 35.5 1200', reply_markup=MAIN_KEYBOARD); return
        try:
            odometer = float(parts[0]); liters = float(parts[1]); cost = float(parts[2])
        except:
            await update.message.reply_text('請輸入數字，例如：12500 35.5 1200', reply_markup=MAIN_KEYBOARD); return
        if liters<=0 or cost<=0:
            await update.message.reply_text('公升數和金額必須大於 0', reply_markup=MAIN_KEYBOARD); return
        last_km = get_last_odometer()
        if last_km and odometer <= last_km:
            await update.message.reply_text(f'⚠️ 里程（{odometer:,.0f}）不能小於上次（{last_km:,.0f}）', reply_markup=MAIN_KEYBOARD); return
        if last_km and odometer > last_km:
            distance = round(odometer - last_km, 1)
            economy  = round((liters / distance) * 100, 2)  # L/100km
            eco_text = f'🛢️ 本次油耗：{economy} L/100km\n📏 行駛距離：{distance:,.0f} km'
        else:
            distance=0; economy=0; eco_text='（首次記錄，下次可計算油耗）'
        date = save_record(odometer, liters, cost, economy, distance)
        await update.message.reply_text(
            f'✅ 記錄完成！\n📅 {date}\n🛣️ 里程：{odometer:,.0f} km\n⛽ 加油：{liters} L\n💰 金額：NT${cost:,.0f}\n💧 油價：NT${round(cost/liters,1)}/L\n{eco_text}',
            reply_markup=MAIN_KEYBOARD); return

    if text == '⛽ 加油記錄':
        last_km = get_last_odometer()
        hint = f'（上次里程：{last_km:,.0f} km）' if last_km else '（首次記錄）'
        context.user_data['waiting_fuel'] = True
        await update.message.reply_text(
            f'📝 請輸入加油資訊 {hint}\n\n格式：`里程 公升 金額`\n例如：`12500 35.5 1200`',
            parse_mode='Markdown', reply_markup=MAIN_KEYBOARD); return

    if text == '📊 油耗查詢':
        s = get_stats()
        if not s: await update.message.reply_text('尚無記錄', reply_markup=MAIN_KEYBOARD); return
        await update.message.reply_text(
            f'📊 油耗統計（共 {s["count"]} 次）\n\n'
            f'🛢️ 最近一次：{s["last"]} L/100km\n'
            f'📈 平均油耗：{s["avg"]} L/100km\n'
            f'🏆 最省油：{s["best"]} L/100km\n'
            f'📉 最耗油：{s["worst"]} L/100km\n\n'
            f'⛽ 累計加油：{s["tl"]} L\n'
            f'📏 累計里程：{s["td"]:,.0f} km\n'
            f'💰 累計花費：NT${s["tc"]:,.0f}',
            reply_markup=MAIN_KEYBOARD); return

    if text == '📋 歷史紀錄':
        records = get_recent(5)
        if not records: await update.message.reply_text('尚無記錄', reply_markup=MAIN_KEYBOARD); return
        lines = ['📋 最近 5 次：\n']
        for r in reversed(records):
            eco = r.get('油耗(L/100km)','-')
            lines.append(f"📅 {str(r.get('日期',''))[:10]}  ⛽{r.get('加油(L)',0)}L  🛢️{eco}L/100km")
        await update.message.reply_text('\n'.join(lines), reply_markup=MAIN_KEYBOARD); return

    if text in ['❓ 說明','說明']:
        await update.message.reply_text(
            '⛽ 加油記錄：一次輸入「里程 公升 金額」\n例如：12500 35.5 1200\n\n'
            '🛢️ 油耗以 L/100km 計算（越小越省油）\n\n'
            '📊 油耗查詢：統計含平均油耗\n📋 歷史紀錄：最近 5 次',
            reply_markup=MAIN_KEYBOARD); return

    await update.message.reply_text('請點選下方按鈕', reply_markup=MAIN_KEYBOARD)


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
