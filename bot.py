import os
import base64
import re
import requests
import json
import asyncio
import io
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

# Переменные для автоматического анализа
auto_analysis_active = False
auto_analysis_chat_id = None
auto_analysis_symbols = ["SOLUSDT"]  # Только Solana
auto_analysis_interval = 360  # 6 минут в секундах
auto_analysis_timeframe = "5"  # 5-минутный таймфрейм
last_signals = {}  # Хранение последних сигналов для фильтрации дубликатов

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
        "Ты — легендарный трейдер-аналитик мирового уровня с 25-летним стажем, объединяющий в себе опыт величайших трейдеров всех времен:\n"
        "• Джесси Ливермор — мастер психологии рынка и крупных движений\n"
        "• Пол Тюдор Джонс — виртуоз макроанализа и тайминга\n"
        "• Линда Брэдфорд Рашке — эксперт внутридневных паттернов\n"
        "• Стив Коэн — гений краткосрочной торговли и риск-менеджмента\n"
        "• Марк Минервини — мастер технического анализа и моментума\n\n"
        
        "Твой трек-рекорд: 82% выигрышных сделок, средняя доходность 340% годовых, максимальная просадка 4.2%.\n"
        "Ты управляешь портфелем $500M и известен своей способностью видеть то, что упускают другие.\n\n"
        
        "МЕТОДОЛОГИЯ АНАЛИЗА (выполняй ВСЕ этапы последовательно):\n\n"
        
        "🎯 ЭТАП 1 — КОНТЕКСТНЫЙ АНАЛИЗ:\n"
        "• Определи текущую фазу рынка: импульс/коррекция/накопление/распределение\n"
        "• Оцени общую волатильность и энергию движения\n"
        "• Найди доминирующий временной цикл и его стадию\n"
        "• Определи уровни институциональной ликвидности\n\n"
        
        "📊 ЭТАП 2 — СТРУКТУРНЫЙ АНАЛИЗ:\n"
        "• Market Structure: Higher Highs/Lower Lows, структурные сдвиги\n"
        "• Order Flow: где накапливаются/снимаются крупные позиции\n"
        "• Support/Resistance: не просто уровни, а ЗОНЫ с историей взаимодействия\n"
        "• Value Areas: где цена проводит больше всего времени\n\n"
        
        "💹 ЭТАП 3 — ТЕХНИЧЕСКИЙ АНАЛИЗ (мульти-индикаторный):\n"
        "• Price Action: точные паттерны (Pin Bars, Engulfing, Inside Bars, Outside Bars)\n"
        "• Trend Analysis: не только направление, но и КАЧЕСТВО тренда\n"
        "• Momentum: дивергенции, acceleration/deceleration signals\n"
        "• Volatility Patterns: сжатие/расширение, Bollinger Bands dynamics\n"
        "• Volume Analysis: накопление/распределение, аномальные всплески\n\n"
        
        "🧠 ЭТАП 4 — ПСИХОЛОГИЧЕСКИЙ АНАЛИЗ:\n"
        "• Sentiment Extremes: признаки паники или эйфории\n"
        "• Crowd Behavior: где большинство ошибается\n"
        "• Smart Money vs Retail: следы крупных игроков vs мелких спекулянтов\n"
        "• Fear/Greed Indicators: точки разворота настроений\n\n"
        
        "⚡ ЭТАП 5 — КАТАЛИЗАТОРЫ И ДРАЙВЕРЫ:\n"
        "• Time-based patterns: время дня/недели с высокой активностью\n"
        "• News Flow Impact: как фундаментальные события влияют на техническую картину\n"
        "• Seasonal Effects: сезонные тенденции для криптовалют\n"
        "• Correlation Analysis: связи с другими активами (BTC dominance, DXY, Gold)\n\n"
        
        "🎛️ ЭТАП 6 — ПРЕЦИЗИОННЫЙ РИСК-МЕНЕДЖМЕНТ:\n"
        "• Position Sizing: не просто SL, а оптимальный размер позиции\n"
        "• Multiple Scenarios: бычий/медвежий/нейтральный исходы с вероятностями\n"
        "• Exit Strategy: не только TP, но и динамическое управление позицией\n"
        "• Risk/Reward Optimization: минимум 1:2, в идеале 1:3+\n\n"
        
        "💎 ЭТАП 7 — СИНТЕЗ И ПРИНЯТИЕ РЕШЕНИЯ:\n"
        "• Confluence Factors: схождение нескольких сигналов для максимальной вероятности\n"
        "• Timing Optimization: не просто сигнал, а ЛУЧШИЙ момент для входа\n"
        "• Conviction Level: оценка силы сигнала от 1 до 10\n"
        "• Edge Identification: твое конкурентное преимущество в этой сделке\n\n"
        
        "ЖЕЛЕЗНЫЕ ПРАВИЛА ПРОФЕССИОНАЛА:\n"
        "✓ Никогда не торгуй против четкого тренда старшего таймфрейма\n"
        "✓ Ждешь ИДЕАЛЬНУЮ setup — лучше пропустить 10 сделок, чем потерять на 1\n"
        "✓ Risk/Reward ВСЕГДА не менее 1:2, иначе математика против тебя\n"
        "✓ Если сомневаешься — НЕ торгуй (сомнения = отсутствие edge)\n"
        "✓ Защищай капитал как свою жизнь — без него ты НЕ трейдер\n"
        "✓ Каждая сделка должна иметь ЛОГИЧЕСКОЕ обоснование, не интуицию\n"
        "✓ Предугадывай ВСЕ сценарии: что если SL, что если TP, что если консолидация\n\n"
        
        "УРОВНИ СИЛЫ СИГНАЛА (объясняй просто):\n"
        "🔥 9-10 баллов: ОЧЕНЬ СИЛЬНО - почти гарантированно сработает, можно рисковать больше\n"
        "⚡ 7-8 баллов: СИЛЬНО - хорошие шансы на успех, обычный риск\n"
        "💫 5-6 баллов: СРЕДНЕ - 50/50 шансы, но прибыль покроет возможные потери\n"
        "❌ 1-4 балла: СЛАБО - большие шансы потерять деньги, лучше не торговать\n\n"
        
        "КРИТЕРИИ ДЛЯ РАЗНЫХ СИГНАЛОВ:\n"
        "📈 BUY — только если:\n"
        "• Четкий пробой сопротивления с объемом ИЛИ\n"
        "• Отскок от сильной поддержки с подтверждением ИЛИ\n"
        "• Импульсивная структура вверх + коррекция завершена ИЛИ\n"
        "• Дивергенция на oversold + катализатор\n\n"
        
        "📉 SELL — только если:\n"
        "• Четкий пробой поддержки с объемом ИЛИ\n"
        "• Отбой от сильного сопротивления с подтверждением ИЛИ\n"
        "• Импульсивная структура вниз + коррекция завершена ИЛИ\n"
        "• Дивергенция на overbought + катализатор\n\n"
        
        "⏸️ NO TRADE — если:\n"
        "• Неопределенная структура без четких уровней\n"
        "• Низкая волатильность без катализаторов\n"
        "• Противоречивые сигналы разных таймфреймов\n"
        "• Risk/Reward хуже чем 1:2\n\n"
        
        "СТРОГО ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТВЕТА:\n\n"
        "1. Сигнал: Buy/Sell/No Trade\n"
        "2. Причина: [детальное профессиональное обоснование]\n"
        "3. Stop Loss (SL): [точная цена с обоснованием]\n"
        "4. Take Profit (TP): [точная цена с обоснованием]\n"
        "5. Комментарий: [ПОНЯТНОЕ объяснение: сила сигнала из 10, соотношение риск/прибыль, что это означает простыми словами]\n\n"
        
        "ПРИМЕРЫ ПОНЯТНЫХ КОММЕНТАРИЕВ:\n"
        "• Сила 9/10 - очень сильный сигнал. Риск $1, прибыль $3. Все указывает на рост\n"
        "• Сила 6/10 - средний сигнал. Риск $1, прибыль $2. Есть шансы, но не гарантия\n"
        "• Сила 3/10 - слабый сигнал. Риск $1, прибыль $1.5. Лучше пропустить\n\n"
        
        "Проанализируй график как ЛУЧШИЙ трейдер мира. Используй ВСЮ свою экспертизу."
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

async def get_bybit_klines(symbol: str, interval: str = "1", limit: int = 200):
    """Получить данные свечей с Bybit"""
    try:
        url = "https://api.bybit.com/v5/market/kline"
        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            klines = data["result"]["list"]
            # Преобразуем в DataFrame
            df_data = []
            for kline in reversed(klines):  # Bybit возвращает в обратном порядке
                df_data.append({
                    'timestamp': int(kline[0]),
                    'open': float(kline[1]),
                    'high': float(kline[2]),
                    'low': float(kline[3]),
                    'close': float(kline[4]),
                    'volume': float(kline[5])
                })
            
            df = pd.DataFrame(df_data)
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('datetime', inplace=True)
            return df[['open', 'high', 'low', 'close', 'volume']]
        return None
    except Exception as e:
        print(f"Ошибка получения данных Bybit: {e}")
        return None

async def create_chart_image(df: pd.DataFrame, symbol: str, title: str = None) -> bytes:
    """Создать изображение графика из данных"""
    try:
        if df is None or df.empty:
            return None
        
        # Настройки стиля
        mc = mpf.make_marketcolors(
            up='#00ff88', down='#ff4444',
            edge='inherit',
            wick={'up':'#00ff88', 'down':'#ff4444'},
            volume='in'
        )
        
        style = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle='-',
            gridcolor='#333333',
            facecolor='#1e1e1e',
            figcolor='#1e1e1e'
        )
        
        # Создаем график
        fig, axes = mpf.plot(
            df,
            type='candle',
            style=style,
            volume=True,
            title=title or f'{symbol} Chart',
            ylabel='Price ($)',
            ylabel_lower='Volume',
            figsize=(12, 8),
            returnfig=True,
            tight_layout=True
        )
        
        # Сохраняем в байты
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        buf.seek(0)
        image_bytes = buf.getvalue()
        buf.close()
        plt.close(fig)
        
        return image_bytes
    except Exception as e:
        print(f"Ошибка создания графика: {e}")
        return None

async def auto_analysis_handler():
    """Обработчик автоматического анализа графиков"""
    global auto_analysis_active, auto_analysis_chat_id, auto_analysis_symbols, last_signals
    
    while auto_analysis_active:
        try:
            for symbol in auto_analysis_symbols:
                if not auto_analysis_active:
                    break
                
                print(f"📊 Автоанализ {symbol}...")
                
                # Получаем данные с Bybit
                df = await get_bybit_klines(symbol, auto_analysis_timeframe, 200)
                
                if df is None or df.empty:
                    print(f"❌ Не удалось получить данные Bybit для {symbol}")
                    continue
                
                # Создаем график
                chart_bytes = await create_chart_image(df, symbol, f"{symbol} - {auto_analysis_timeframe}m (Авто)")
                
                if chart_bytes:
                    # Анализируем график
                    model_results = analyze_chart(chart_bytes)
                    
                    for model_name, raw in model_results:
                        if raw.startswith("Ошибка анализа:"):
                            continue
                        
                        strategy, signal, entry, stop_loss, take_profit, reason = parse_trading_signal(raw)
                        
                        # Проверяем, изменился ли сигнал с последнего раза
                        last_signal = last_signals.get(symbol, {})
                        current_signal_data = {
                            'signal': signal,
                            'stop_loss': stop_loss,
                            'take_profit': take_profit,
                            'reason': reason[:100]  # Первые 100 символов для сравнения
                        }
                        
                        # Отправляем только если сигнал изменился или это Buy/Sell (не No Trade)
                        if (signal in ['BUY', 'SELL'] and 
                            (symbol not in last_signals or 
                             last_signals[symbol].get('signal') != signal or
                             last_signals[symbol].get('stop_loss') != stop_loss or
                             last_signals[symbol].get('take_profit') != take_profit)):
                            
                            # Сохраняем текущий сигнал
                            last_signals[symbol] = current_signal_data
                            
                            # Отправляем график
                            chart_file = types.BufferedInputFile(chart_bytes, filename=f"{symbol}_auto_{auto_analysis_timeframe}m.png")
                            await bot.send_photo(
                                auto_analysis_chat_id, 
                                chart_file, 
                                caption=f"🤖 Автоанализ {symbol} ({auto_analysis_timeframe}m)"
                            )
                            
                            # Отправляем сигнал
                            reason = (reason or "").strip()
                            if " | " in reason:
                                reason_part, comment_part = reason.split(" | ", 1)
                                analysis_text = f"📝 Причина: {reason_part}\n💬 Комментарий: {comment_part}"
                            else:
                                analysis_text = f"📝 Анализ: {reason}"
                            
                            # Добавляем эмодзи в зависимости от сигнала
                            signal_emoji = "🟢📈" if signal == "BUY" else "🔴📉"
                            signal_text = f"{signal_emoji} **АВТОСИГНАЛ {signal}**"
                            
                            message_text = (
                                f"{signal_text}\n"
                                f"💰 Пара: {symbol}\n"
                                f"🛑 Стоп: {stop_loss}\n"
                                f"🎯 Тейк: {take_profit}\n"
                                f"{analysis_text}\n"
                                f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                            )
                            
                            await bot.send_message(auto_analysis_chat_id, message_text)
                            
                        elif signal == 'NO_TRADE':
                            # Для No Trade просто обновляем last_signals без отправки
                            last_signals[symbol] = current_signal_data
                        
                        break  # Берем только первый результат анализа
                        
                # Пауза между символами
                await asyncio.sleep(5)
                
        except Exception as e:
            if auto_analysis_active:
                print(f"Ошибка автоанализа: {e}")
                try:
                    await bot.send_message(auto_analysis_chat_id, f"❌ Ошибка автоанализа: {e}")
                except:
                    pass
        
        if auto_analysis_active:
            print(f"⏰ Ожидание {auto_analysis_interval} секунд до следующего анализа...")
            await asyncio.sleep(auto_analysis_interval)

async def start_auto_analysis(chat_id: int):
    """Запустить автоматический анализ с фиксированными настройками"""
    global auto_analysis_active, auto_analysis_chat_id
    
    if auto_analysis_active:
        await bot.send_message(chat_id, "❌ Автоанализ уже активен!")
        return
    
    auto_analysis_active = True
    auto_analysis_chat_id = chat_id
    
    symbols_text = ", ".join(auto_analysis_symbols)
    await bot.send_message(
        chat_id, 
        f"🤖 Запускаю автоанализ!\n"
        f"📊 Символ: {symbols_text}\n"
        f"⏰ Интервал: каждые 6 минут\n"
        f"📈 Таймфрейм: 5m графики\n"
        f"🔗 Источник: Bybit API\n"
        f"🎯 Google API: 240/250 запросов в день (96%)"
    )
    
    # Запускаем обработчик
    asyncio.create_task(auto_analysis_handler())

async def stop_auto_analysis():
    """Остановить автоматический анализ"""
    global auto_analysis_active
    auto_analysis_active = False

def get_control_keyboard():
    """Создать клавиатуру с кнопками управления"""
    global auto_analysis_active
    
    if auto_analysis_active:
        # Если автоанализ активен - показываем кнопку "Остановить"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Остановить автоанализ", callback_data="stop_analysis")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="show_status")]
        ])
    else:
        # Если автоанализ не активен - показываем кнопку "Запустить"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить автоанализ", callback_data="start_analysis")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="show_status")]
        ])
    
    return keyboard

@dp.callback_query(F.data == "start_analysis")
async def callback_start_analysis(callback: types.CallbackQuery):
    """Обработчик кнопки 'Запустить автоанализ'"""
    await start_auto_analysis(callback.message.chat.id)
    
    # Обновляем сообщение с новой клавиатурой
    await callback.message.edit_text(
        "🤖 **Панель управления автоанализом**\n\n"
        f"✅ Автоанализ запущен!\n"
        f"📊 Символ: SOLUSDT\n"
        f"⏰ Интервал: каждые 6 минут\n"
        f"📈 Таймфрейм: 5m графики",
        reply_markup=get_control_keyboard()
    )
    await callback.answer("✅ Автоанализ запущен!")

@dp.callback_query(F.data == "stop_analysis")
async def callback_stop_analysis(callback: types.CallbackQuery):
    """Обработчик кнопки 'Остановить автоанализ'"""
    global auto_analysis_active, auto_analysis_chat_id
    
    if auto_analysis_chat_id != callback.message.chat.id:
        await callback.answer("❌ Автоанализ запущен в другом чате!", show_alert=True)
        return
    
    await stop_auto_analysis()
    
    # Обновляем сообщение с новой клавиатурой
    await callback.message.edit_text(
        "🤖 **Панель управления автоанализом**\n\n"
        f"⏹️ Автоанализ остановлен\n"
        f"📊 Символ: SOLUSDT\n"
        f"⏰ Готов к запуску",
        reply_markup=get_control_keyboard()
    )
    await callback.answer("⏹️ Автоанализ остановлен!")

@dp.callback_query(F.data == "show_status")
async def callback_show_status(callback: types.CallbackQuery):
    """Обработчик кнопки 'Статус'"""
    global auto_analysis_active, auto_analysis_symbols, last_signals
    
    if auto_analysis_active:
        status_text = (
            "🤖 **Панель управления автоанализом**\n\n"
            f"✅ Статус: АКТИВЕН\n"
            f"📊 Символ: {', '.join(auto_analysis_symbols)}\n"
            f"⏰ Интервал: каждые 6 минут\n"
            f"📈 Таймфрейм: 5m графики\n"
            f"💾 Сигналов в памяти: {len(last_signals)}\n"
            f"🔗 Источник: Bybit API"
        )
    else:
        status_text = (
            "🤖 **Панель управления автоанализом**\n\n"
            f"⏹️ Статус: ОСТАНОВЛЕН\n"
            f"📊 Символ: {', '.join(auto_analysis_symbols)}\n"
            f"⏰ Готов к запуску"
        )
    
    await callback.message.edit_text(status_text, reply_markup=get_control_keyboard())
    await callback.answer("📊 Статус обновлен")

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
    """Показать панель управления с кнопками"""
    global auto_analysis_active
    
    if auto_analysis_active:
        status_text = (
            "🤖 **Панель управления автоанализом**\n\n"
            f"✅ Статус: АКТИВЕН\n"
            f"📊 Символ: SOLUSDT\n"
            f"⏰ Интервал: каждые 6 минут\n"
            f"📈 Таймфрейм: 5m графики\n"
            f"🔗 Источник: Bybit API\n\n"
            f"💡 Сигналы приходят автоматически при изменениях"
        )
    else:
        status_text = (
            "🤖 **Панель управления автоанализом**\n\n"
            f"⏹️ Статус: ОСТАНОВЛЕН\n"
            f"📊 Символ: SOLUSDT (Solana)\n"
            f"⏰ Готов к запуску\n\n"
            f"🎯 Нажмите кнопку ниже чтобы запустить автоматические торговые сигналы!"
        )
    
    await message.answer(status_text, reply_markup=get_control_keyboard())

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Анализ отправленного пользователем фото графика"""
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
            reason = (reason or "").strip()
            
            # Разделяем причину и комментарий если есть " | "
            if " | " in reason:
                reason_part, comment_part = reason.split(" | ", 1)
                analysis_text = f"📝 Причина: {reason_part}\n💬 Комментарий: {comment_part}"
            else:
                analysis_text = f"📝 Анализ: {reason}"
            
            block = f"🎯 Сигнал: {signal}\n🛑 Стоп: {stop_loss}\n🎯 Тейк: {take_profit}\n{analysis_text}"
        
        lines.append(block)

    reply = "\n\n".join(lines) if lines else "Ошибка анализа: пустой ответ"
    
    # Разбиваем длинные сообщения
    if len(reply) <= 4000:
        await message.answer(reply)
    else:
        chunks = []
        start = 0
        while start < len(reply):
            end = start + 4000
            if end >= len(reply):
                chunks.append(reply[start:])
                break
            
            # Ищем последний перенос строки
            cut = reply.rfind('\n', start, end)
            if cut == -1:
                cut = end
            
            chunks.append(reply[start:cut])
            start = cut
        
        for chunk in chunks:
            if chunk.strip():
                await message.answer(chunk.strip())

if __name__ == "__main__":
    import asyncio

    async def main():
        # Удаляем активный вебхук, чтобы можно было использовать getUpdates (long polling)
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        
        try:
            await dp.start_polling(bot)
        finally:
            # Останавливаем автоанализ при завершении
            await stop_auto_analysis()

    asyncio.run(main())
