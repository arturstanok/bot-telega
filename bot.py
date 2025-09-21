import os
import base64
import re
import requests
import json
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
PROXY_URL = os.getenv("PROXY_URL")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN environment variable")
if not GOOGLE_API_KEY:
    raise RuntimeError("Set GOOGLE_API_KEY environment variable")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Счетчик запросов
request_count = 0
request_log = []

# Загружаем существующий лог при запуске
def load_request_log():
    global request_count, request_log
    try:
        if os.path.exists("request_log.json"):
            with open("request_log.json", "r", encoding="utf-8") as f:
                request_log = json.load(f)
                request_count = len(request_log)
        else:
            request_log = []
            request_count = 0
    except Exception:
        request_log = []
        request_count = 0

# Загружаем лог при импорте
load_request_log()

# Лимиты Google
GOOGLE_LIMITS = {"daily": 250, "monthly": 7500, "period": "день"}

def get_pacific_time():
    """Получить текущее время в тихоокеанском часовом поясе"""
    pacific_tz = timezone(timedelta(hours=-8))
    return datetime.now(pacific_tz)

def should_reset_google_counter():
    """Проверить, нужно ли сбросить счетчик Google (прошла полночь PT)"""
    try:
        if os.path.exists("last_reset.json"):
            with open("last_reset.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                last_reset = datetime.fromisoformat(data["last_reset"])
        else:
            return True
        
        pacific_now = get_pacific_time()
        return pacific_now.date() > last_reset.date()
    except:
        return True

def reset_google_counter():
    """Сбросить счетчик Google запросов"""
    global request_log
    pacific_now = get_pacific_time()
    
    # Удаляем все записи Google за предыдущие дни
    request_log = [log for log in request_log if not (
        log["provider"] == "google" and 
        datetime.fromisoformat(log["timestamp"]).date() < pacific_now.date()
    )]
    
    # Сохраняем время последнего сброса
    with open("last_reset.json", "w", encoding="utf-8") as f:
        json.dump({"last_reset": pacific_now.isoformat()}, f, ensure_ascii=False, indent=2)
    
    # Сохраняем обновленный лог
    with open("request_log.json", "w", encoding="utf-8") as f:
        json.dump(request_log, f, ensure_ascii=False, indent=2)

def log_request(provider: str, model: str, success: bool):
    global request_count
    request_count += 1
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    request_log.append({
        "timestamp": timestamp,
        "provider": provider,
        "model": model,
        "success": success,
        "count": request_count
    })
    with open("request_log.json", "w", encoding="utf-8") as f:
        json.dump(request_log, f, ensure_ascii=False, indent=2)

def _call_google(image_bytes: bytes, question: str, model_name: str) -> str:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GOOGLE_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": question},
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
                ]
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1000000}
    }
    
    # Пробуем сначала с прокси, потом без прокси
    proxy_configs = []
    if PROXY_URL:
        proxy_configs.append({
            "http": PROXY_URL,
            "https": PROXY_URL
        })
    proxy_configs.append(None)  # Без прокси
    
    for proxies in proxy_configs:
        try:
            resp = requests.post(url, headers=headers, json=body, proxies=proxies, timeout=120)
            if resp.status_code != 200:
                continue
            
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                continue
            
            parts = (candidates[0].get("content") or {}).get("parts") or []
            texts = []
            for p in parts:
                t = p.get("text") if isinstance(p, dict) else None
                if isinstance(t, str):
                    texts.append(t)
            result = "\n".join(texts).strip()
            log_request("google", model_name, True)
            return result
            
        except Exception as e:
            if proxies is None:
                log_request("google", model_name, False)
                return f"Ошибка анализа: Google({model_name}) exception: {e}"
            continue
    
    log_request("google", model_name, False)
    return "Ошибка анализа: Google все конфигурации не сработали"

def analyze_chart(image_bytes):
    # Проверяем и сбрасываем счетчик Google если нужно
    if should_reset_google_counter():
        reset_google_counter()
    
    question = (
        "Ты — профессиональный трейдер‑аналитик, специализирующийся на внутридневной торговле и скальпинге.\n"
        "Тебе дан скриншот графика.\n\n"
        
        "Твоя задача — проанализировать график и найти торговые возможности.\n"
        "Ищи четкие сигналы: пробои уровней, отбои от поддержки/сопротивления, трендовые движения и тд.\n"
        "Сделка должна быть краткосрочной: максимум 1–2 часа удержания позиции.\n\n"
        
        "АНАЛИЗИРУЙ ВНИМАТЕЛЬНО:\n"
        "- Тренд\n"
        "- Ключевые уровни поддержки и сопротивления\n"
        "- Свечные паттерны и формации\n"
        "- Объем торгов\n"
        "- Волатильность\n\n"
        
        "Формат ответа строго такой:\n\n"
        "1. Сигнал: Buy\n"
        "2. Причина: пробой сопротивления с объемом\n"
        "3. Stop Loss (SL): 234.20\n"
        "4. Take Profit (TP): 236.50\n"
        "5. Комментарий: сильный сигнал\n\n"
        "ИЛИ\n\n"
        "1. Сигнал: Sell\n"
        "2. Причина: отбой от сопротивления\n"
        "3. Stop Loss (SL): 235.80\n"
        "4. Take Profit (TP): 233.20\n"
        "5. Комментарий: средний сигнал\n\n"
        "ИЛИ\n\n"
        "1. Сигнал: No Trade\n"
        "2. Причина: флетовое движение\n"
        "3. Stop Loss (SL): -\n"
        "4. Take Profit (TP): -\n"
        "5. Комментарий: неопределенная ситуация\n\n"
        
        "ВАЖНО:\n"
        "- Будь активным в поиске сигналов\n"
        "- No Trade только если действительно нет четких возможностей\n"
        "- Анализируй только то, что видно на графике, не придумывай данные и сигналы\n"
        "- Сигналы должны быть реалистичными для внутридневной торговли и скальпинга\n"
        "- Stop Loss (SL): укажи уровень или зону, где логично поставить SL\n"
        "- Take Profit (TP): укажи уровень или зону для TP\n"
        "- Отвечай ТОЛЬКО в указанном формате\n\n"
        
        "ПРИМЕРЫ СИГНАЛОВ:\n"
        "- Если цена пробила сопротивление вверх → Buy\n"
        "- Если цена отбилась от сопротивления вниз → Sell\n"
        "- Если цена отскочила от поддержки вверх → Buy\n"
        "- Если цена пробила поддержку вниз → Sell\n"
        "- Если четкий тренд вверх → Buy\n"
        "- Если четкий тренд вниз → Sell\n"
        "- Только если полный флет без уровней → No Trade\n\n"
        
        "Проанализируй график и анализ должен быть в одно предложение:"
    )
    
    raw = _call_google(image_bytes, question, GOOGLE_MODEL)
    return [(f"google/{GOOGLE_MODEL}", raw)]

def parse_trading_signal(text: str) -> tuple[str, str, str, str, str, str]:
    # Ищем сигнал
    signal_patterns = [
        r"^\s*1\.\s*Сигнал:\s*(Buy|Sell|No Trade)\b",
        r"^\s*1\.\s*\*\*Сигнал:\*\*\s*(Buy|Sell|No Trade)\b",
        r"Сигнал:\s*(Buy|Sell|No Trade)\b",
        r"\*\*Сигнал:\*\*\s*(Buy|Sell|No Trade)\b"
    ]
    
    signal = "NO_TRADE"
    for pattern in signal_patterns:
        m_sig = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if m_sig:
            signal = m_sig.group(1).upper()
            break
    
    # Ищем причину
    reason_patterns = [
        r"^\s*2\.\s*Причина:\s*(.*?)(?=\n\s*[3-5]\.|$)",
        r"^\s*2\.\s*\*\*Причина:\*\*\s*(.*?)(?=\n\s*[3-5]\.|$)",
        r"Причина:\s*(.*?)(?=\n\s*[3-5]\.|$)",
        r"\*\*Причина:\*\*\s*(.*?)(?=\n\s*[3-5]\.|$)"
    ]
    
    reason = ""
    for pattern in reason_patterns:
        m_rea = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m_rea:
            reason = m_rea.group(1).strip()
            break
    
    # Ищем стоп-лосс
    sl_patterns = [
        r"^\s*3\.\s*Stop Loss \(SL\):\s*(.*?)(?=\n\s*[4-5]\.|$)",
        r"Stop Loss \(SL\):\s*(.*?)(?=\n\s*[4-5]\.|$)",
        r"Stop Loss:\s*(.*?)(?=\n\s*[4-5]\.|$)",
        r"SL:\s*(.*?)(?=\n\s*[4-5]\.|$)"
    ]
    
    stop_loss = "-"
    for pattern in sl_patterns:
        m_sl = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m_sl:
            stop_loss = m_sl.group(1).strip()
            break
    
    # Ищем тейк-профит
    tp_patterns = [
        r"^\s*4\.\s*Take Profit \(TP\):\s*(.*?)(?=\n\s*5\.|$)",
        r"Take Profit \(TP\):\s*(.*?)(?=\n\s*5\.|$)",
        r"Take Profit:\s*(.*?)(?=\n\s*5\.|$)",
        r"TP:\s*(.*?)(?=\n\s*5\.|$)"
    ]
    
    take_profit = "-"
    for pattern in tp_patterns:
        m_tp = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m_tp:
            take_profit = m_tp.group(1).strip()
            break
    
    # Ищем комментарий
    comm_patterns = [
        r"^\s*5\.\s*Комментарий:\s*(.*?)$",
        r"Комментарий:\s*(.*?)$",
        r"\*\*Комментарий:\*\*\s*(.*?)$"
    ]
    
    comment = ""
    for pattern in comm_patterns:
        m_comm = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m_comm:
            comment = m_comm.group(1).strip()
            break
    
    # Если причина пустая, ищем в тексте
    if not reason:
        lines = text.splitlines()
        for line in lines:
            line = line.strip()
            if line and not re.match(r"^\s*[1-5]\.\s*(Сигнал|Причина|Stop Loss|Take Profit|Комментарий):\s*", line, flags=re.IGNORECASE):
                if not re.match(r"^\s*\*\*.*\*\*:\s*", line):
                    reason = line
                    break
    
    # Последний фолбэк - весь текст
    if not reason:
        reason = text.strip()
    
    # Объединяем причину и комментарий только если они разные
    full_reason = reason
    if comment and comment != reason and comment not in reason:
        full_reason = f"{reason} | {comment}"
    
    return "TRADING", signal, "-", stop_loss, take_profit, full_reason[:300]

def _chunk_text(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    
    chunks = []
    start = 0
    while start < len(text):
        end = start + limit
        if end >= len(text):
            chunks.append(text[start:].strip())
            break
        
        # Ищем последний пробел или перенос строки
        cut = text.rfind('\n', start, end)
        if cut == -1:
            cut = text.rfind(' ', start, end)
        if cut == -1:
            cut = end
        
        chunks.append(text[start:cut].strip())
        start = cut
    return [c for c in chunks if c]

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Показать статистику запросов"""
    # Проверяем и сбрасываем счетчик Google если нужно
    if should_reset_google_counter():
        reset_google_counter()
    
    if not request_log:
        await message.answer("📊 Статистика пуста - запросов еще не было")
        return
    
    # Подсчет по провайдерам
    providers = {}
    successful = 0
    failed = 0
    
    for log in request_log:
        provider = log["provider"]
        if provider not in providers:
            providers[provider] = {"success": 0, "failed": 0}
        
        if log["success"]:
            providers[provider]["success"] += 1
            successful += 1
        else:
            providers[provider]["failed"] += 1
            failed += 1
    
    # Формируем ответ
    stats_text = f"📊 **Статистика запросов**\n\n"
    stats_text += f"🔢 **Всего запросов:** {request_count}\n"
    stats_text += f"✅ **Успешных:** {successful}\n"
    stats_text += f"❌ **Неудачных:** {failed}\n\n"
    
    stats_text += "📈 **По провайдерам:**\n"
    for provider, counts in providers.items():
        total = counts["success"] + counts["failed"]
        success_rate = (counts["success"] / total * 100) if total > 0 else 0
        
        # Получаем лимиты для провайдера
        limits = GOOGLE_LIMITS if provider == "google" else {}
        if limits:
            daily_limit = limits.get("daily", 0)
            monthly_limit = limits.get("monthly", 0)
            period = limits.get("period", "день")
            
            # Рассчитываем использование
            if period == "день":
                usage_percent = (total / daily_limit * 100) if daily_limit > 0 else 0
                limit_text = f"({total}/{daily_limit} в день, {usage_percent:.1f}%)"
            else:
                usage_percent = (total / monthly_limit * 100) if monthly_limit > 0 else 0
                limit_text = f"({total}/{monthly_limit} в месяц, {usage_percent:.1f}%)"
            
            stats_text += f"• **{provider}:** {total} ({counts['success']}✅/{counts['failed']}❌) - {success_rate:.1f}% {limit_text}\n"
        else:
            stats_text += f"• **{provider}:** {total} ({counts['success']}✅/{counts['failed']}❌) - {success_rate:.1f}%\n"
    
    # Последние 5 запросов
    stats_text += f"\n🕒 **Последние 5 запросов:**\n"
    for log in request_log[-5:]:
        status = "✅" if log["success"] else "❌"
        stats_text += f"{status} {log['timestamp']} - {log['provider']}/{log['model']}\n"
    
    # Добавляем ссылки на проверку лимитов
    stats_text += f"\n🔗 **Проверить лимиты:**\n"
    stats_text += f"• [Google AI Studio](https://aistudio.google.com/usage?project=carbon-crossing-470508-p7)\n"
    stats_text += f"  🕐 Сброс: полночь PT (UTC-8)\n"
    
    await message.answer(stats_text)

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Привет! Отправь мне фото графика, и я дам торговую рекомендацию с точками входа, стопом и тейком 📈📉")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer("Анализирую график… ⏳")
    
    # Получаем изображение
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    img_bytes = await bot.download_file(file.file_path)
    
    # Анализируем график
    model_results = analyze_chart(img_bytes.read())
    
    lines = []
    for model_name, raw in model_results:
        if raw.startswith("Ошибка анализа:"):
            block = f"🧠 {model_name}: {raw}"
        else:
            strategy, signal, entry, stop_loss, take_profit, reason = parse_trading_signal(raw)
            # hard cap reason length for safety
            reason = (reason or "").strip()
            if len(reason) > 400:
                reason = reason[:397] + "…"
            
            # Добавляем информацию о лимитах
            limits = GOOGLE_LIMITS if model_name.startswith("google/") else {}
            limit_info = ""
            if limits:
                daily_limit = limits.get("daily", 0)
                monthly_limit = limits.get("monthly", 0)
                period = limits.get("period", "день")
                
                # Подсчитываем использование для этого провайдера
                provider_usage = sum(1 for log in request_log if log["provider"] == model_name.split("/")[0])
                
                if period == "день":
                    usage_percent = (provider_usage / daily_limit * 100) if daily_limit > 0 else 0
                    limit_info = f"\n\n📈 Лимит: {provider_usage}/{daily_limit} в день ({usage_percent:.1f}%)\n🕐 Сброс: в 11.00\n🔗 Проверить: https://aistudio.google.com/usage?project=carbon-crossing-470508-p7"
                else:
                    usage_percent = (provider_usage / monthly_limit * 100) if monthly_limit > 0 else 0
                    limit_info = f"\n\n📈 Лимит: {provider_usage}/{monthly_limit} в месяц ({usage_percent:.1f}%)\n🔗 Проверить: https://aistudio.google.com/usage?project=carbon-crossing-470508-p7"
            
            # Разделяем причину и комментарий если есть " | "
            if " | " in reason:
                reason_part, comment_part = reason.split(" | ", 1)
                analysis_text = f"📝 Причина: {reason_part}\n💬 Комментарий: {comment_part}"
            else:
                analysis_text = f"📝 Анализ: {reason}"
            
            block = f"🎯 Сигнал: {signal}\n🛑 Стоп: {stop_loss}\n🎯 Тейк: {take_profit}\n{analysis_text}{limit_info}"
        
        # Each block also capped to avoid exploding length
        if len(block) > 1000:
            block = block[:997] + "…"
        lines.append(block)

    reply = "\n\n".join(lines) if lines else "Ошибка анализа: пустой ответ"

    for part in _chunk_text(reply):
        await message.answer(part)

if __name__ == "__main__":
    import asyncio

    async def main():
        # Удаляем активный вебхук, чтобы можно было использовать getUpdates (long polling)
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await dp.start_polling(bot)

    asyncio.run(main())