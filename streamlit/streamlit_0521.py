import streamlit as st
import sqlite3
import requests
import json
import datetime
import time
import pandas as pd
import torch
import torch.nn as nn
from sklearn.preprocessing import MinMaxScaler

# model py파일 import
from model1 import load_model
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

# "C:/Users/ssb70/OneDrive/바탕 화면/캡스톤깃헙/Autotrade/streamlit_0521.py"

# 데이터베이스 연결
conn = sqlite3.connect('stock_prices.db')
cursor = conn.cursor()

# 테이블에 'close'와 'volume' 열이 존재하는지 확인하고, 없으면 추가
cursor.execute("PRAGMA table_info(price_info)")
columns = [info[1] for info in cursor.fetchall()]
if 'close' not in columns:
    cursor.execute('ALTER TABLE price_info ADD COLUMN close INTEGER')
if 'volume' not in columns:
    cursor.execute('ALTER TABLE price_info ADD COLUMN volume INTEGER')
conn.commit()

# 테이블 생성 (필요할 경우)
cursor.execute('''
CREATE TABLE IF NOT EXISTS price_info (
    time_key TEXT,
    stock_code TEXT,
    high INTEGER,
    low INTEGER,
    open INTEGER,
    close INTEGER,
    volume INTEGER,
    PRIMARY KEY (time_key, stock_code)
)
''')
conn.commit()

# 모델1 부분

input_dim = 8  
hidden_dim = 32
num_layers = 2
output_dim = 1
model1 = load_model('stock_predictor_model.pth', input_dim, hidden_dim, num_layers, output_dim)

def get_model_prediction(stock_code, current_hour_key):
    # current_hour_key 이전 10개 데이터 가져오기
    cursor.execute('SELECT * FROM price_info WHERE stock_code = ? AND time_key < ? ORDER BY time_key DESC LIMIT 10', (stock_code, current_hour_key))
    rows = cursor.fetchall()

    if len(rows) < 10:
        return None  # 데이터가 충분하지 않으면 None 반환

    # 데이터를 DataFrame으로 변환

    df = pd.DataFrame(rows, columns=['time_key', 'stock_code', 'high', 'low', 'open', 'close', 'Volume'])
    df['H-L'] = df['high'] - df['low']
    df['O-C'] = df['close'] - df['open']
    df['2_HOURS_MA'] = df['close'].rolling(window=2, min_periods=1).mean()
    df['4_HOURS_MA'] = df['close'].rolling(window=4, min_periods=1).mean()
    df['6_HOURS_MA'] = df['close'].rolling(window=6, min_periods=1).mean()
    df['3_HOURS_STD_DEV'] = df['close'].rolling(window=3, min_periods=1).std()

    # 특징 선택 및 정규화
    features = df[['H-L', 'O-C', '2_HOURS_MA', '4_HOURS_MA', '6_HOURS_MA', '3_HOURS_STD_DEV', 'Volume', 'close']].values
    scaler = MinMaxScaler(feature_range=(0, 1))
    scaled_features = scaler.fit_transform(features)

    # 시퀀스 데이터 생성
    X_input = torch.tensor(scaled_features.reshape(1, 10, -1), dtype=torch.float32).to(device)
    
    # 모델 예측
    model1.eval()
    with torch.no_grad():
        output = model1(X_input)
        prediction = torch.sigmoid(output).item()
        predicted_class = 1 if prediction > 0.475 else 0  # 예측값 임계치 기준으로 클래스 결정

    return predicted_class


# 전역 변수 설정
ACCESS_TOKEN = None
token_issue_time = None
TOKEN_VALIDITY_DURATION = 3600 * 6  # 토큰 유효 기간 (6시간)

# 함수 정의
def send_message(msg, DISCORD_WEBHOOK_URL):
    """디스코드 메세지 전송"""
    now = datetime.datetime.now()
    message = {"content": f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {str(msg)}"}
    requests.post(DISCORD_WEBHOOK_URL, data=message)
    print(message)

def get_access_token(APP_KEY, APP_SECRET, URL_BASE):
    """토큰 발급"""
    global ACCESS_TOKEN, token_issue_time
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    PATH = "oauth2/tokenP"
    URL = f"{URL_BASE}/{PATH}"
    res = requests.post(URL, headers=headers, data=json.dumps(body))
    if res.status_code == 200:
        ACCESS_TOKEN = res.json()["access_token"]
        token_issue_time = datetime.datetime.now()
        return ACCESS_TOKEN
    else:
        raise Exception("Failed to get access token")

def ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE):
    """토큰의 유효성을 보장하고 필요시 갱신"""
    global ACCESS_TOKEN, token_issue_time
    if ACCESS_TOKEN is None or (datetime.datetime.now() - token_issue_time).total_seconds() >= TOKEN_VALIDITY_DURATION:
        ACCESS_TOKEN = get_access_token(APP_KEY, APP_SECRET, URL_BASE)

def hashkey(datas, APP_KEY, APP_SECRET, URL_BASE):
    """암호화"""
    PATH = "uapi/hashkey"
    URL = f"{URL_BASE}/{PATH}"
    headers = {
        'content-Type': 'application/json',
        'appKey': APP_KEY,
        'appSecret': APP_SECRET,
    }
    res = requests.post(URL, headers=headers, data=json.dumps(datas))
    hashkey = res.json()["HASH"]
    return hashkey

def get_current_price_and_volume(code, APP_KEY, APP_SECRET, URL_BASE):
    """현재가와 누적 거래량 조회"""
    ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
    PATH = "uapi/domestic-stock/v1/quotations/inquire-price"
    URL = f"{URL_BASE}/{PATH}"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "FHKST01010100"
    }
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": code,
    }
    res = requests.get(URL, headers=headers, params=params)
    data = res.json()['output']
    current_price = int(data['stck_prpr'])
    acml_vol = int(data['acml_vol'])
    return current_price, acml_vol

def get_previous_row(stock_code, current_hour_key):
    cursor.execute('''
        SELECT high, low 
        FROM price_info 
        WHERE stock_code = ? AND time_key < ? 
        ORDER BY time_key DESC 
        LIMIT 1
    ''', (stock_code, current_hour_key))
    return cursor.fetchone()

def get_target_price_change(stock_code, k):
    now = datetime.datetime.now()
    current_hour_key = now.strftime('%Y-%m-%d %H')

    if now.hour == 9:
        previous_day = now - datetime.timedelta(days=1)
        previous_hour_key = previous_day.replace(hour=14).strftime('%Y-%m-%d %H')
        cursor.execute('SELECT high, low FROM price_info WHERE time_key = ? AND stock_code = ?', (previous_hour_key, stock_code))
        prev_data = cursor.fetchone()
        if not prev_data:
            prev_data = get_previous_row(stock_code, current_hour_key)  # 수정된 부분
    else:
        previous_hour = now - datetime.timedelta(hours=1)
        previous_hour_key = previous_hour.strftime('%Y-%m-%d %H')
        cursor.execute('SELECT high, low FROM price_info WHERE time_key = ? AND stock_code = ?', (previous_hour_key, stock_code))
        prev_data = cursor.fetchone()
        if not prev_data:
            prev_data = get_previous_row(stock_code, current_hour_key)  # 수정된 부분

    cursor.execute('SELECT open FROM price_info WHERE time_key = ? AND stock_code = ?', (current_hour_key, stock_code))
    stck_oprc = cursor.fetchone()
    
    # k는 변동성 돌파 전략의 비례계수
    if stck_oprc and prev_data:
        stck_oprc = stck_oprc[0]
        stck_hgpr, stck_lwpr = prev_data
        target_price = stck_oprc + (stck_hgpr - stck_lwpr) * k
        return target_price
    else:
        return None



def get_target_price_ma(stock_code):
    cursor.execute('SELECT time_key, open FROM price_info WHERE stock_code = ?', (stock_code,))
    rows = cursor.fetchall()
    df = pd.DataFrame(rows, columns=['time_key', 'open'])
    df['time_key'] = pd.to_datetime(df['time_key'])
    df.set_index('time_key', inplace=True)
    df['SMA2'] = df['open'].rolling(window=2, min_periods=1).mean()
    df['SMA4'] = df['open'].rolling(window=4, min_periods=1).mean()
    return df

def get_accumulated_volume(stock_code, current_hour_key):
    current_date = current_hour_key.split(' ')[0]  # YYYY-MM-DD 부분만 추출
    cursor.execute('''
        SELECT SUM(volume) 
        FROM price_info 
        WHERE stock_code = ? AND time_key < ? AND time_key LIKE ?
    ''', (stock_code, current_hour_key, f'{current_date}%'))
    result = cursor.fetchone()
    return result[0] if result[0] is not None else 0


def update_price_info(current_price, current_volume, current_time, stock_code):
    time_key = current_time.strftime('%Y-%m-%d %H')
    
    cursor.execute('SELECT * FROM price_info WHERE time_key = ? AND stock_code = ?', (time_key, stock_code))
    data = cursor.fetchone()

    # 누적 거래량 계산
    accumulated_volume_before = get_accumulated_volume(stock_code, time_key)
    volume = current_volume - accumulated_volume_before

    if data is None:
        cursor.execute('''
        INSERT INTO price_info (time_key, stock_code, high, low, open, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (time_key, stock_code, current_price, current_price, current_price, current_price, volume))
        conn.commit()
    else:
        cursor.execute('''
        UPDATE price_info
        SET high = MAX(high, ?),
            low = MIN(low, ?),
            close = ?,
            volume = ?
        WHERE time_key = ? AND stock_code = ?
        ''', (current_price, current_price, current_price, volume, time_key, stock_code))
        conn.commit()


def get_stock_balance(APP_KEY, APP_SECRET, URL_BASE):
    """주식 잔고조회"""
    ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
    PATH = "uapi/domestic-stock/v1/trading/inquire-balance"
    URL = f"{URL_BASE}/{PATH}"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "TTTC8434R",
        "custtype": "P"
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    res = requests.get(URL, headers=headers, params=params)
    stock_list = res.json()['output1']
    evaluation = res.json()['output2']
    stock_dict = {}
    send_message(f"====주식 보유잔고====", DISCORD_WEBHOOK_URL)
    for stock in stock_list:
        if int(stock['hldg_qty']) > 0:
            stock_dict[stock['pdno']] = stock['hldg_qty']
            send_message(f"{stock['prdt_name']}({stock['pdno']}): {stock['hldg_qty']}주", DISCORD_WEBHOOK_URL)
            time.sleep(0.1)
    send_message(f"주식 평가 금액: {evaluation[0]['scts_evlu_amt']}원", DISCORD_WEBHOOK_URL)
    time.sleep(0.1)
    send_message(f"평가 손익 합계: {evaluation[0]['evlu_pfls_smtl_amt']}원", DISCORD_WEBHOOK_URL)
    time.sleep(0.1)
    send_message(f"총 평가 금액: {evaluation[0]['tot_evlu_amt']}원", DISCORD_WEBHOOK_URL)
    time.sleep(0.1)
    send_message(f"=================", DISCORD_WEBHOOK_URL)
    return stock_dict

def get_balance(APP_KEY, APP_SECRET, URL_BASE):
    """현금 잔고조회"""
    ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
    PATH = "uapi/domestic-stock/v1/trading/inquire-psbl-order"
    URL = f"{URL_BASE}/{PATH}"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "TTTC8908R",
        "custtype": "P"
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": "005930",
        "ORD_UNPR": "65500",
        "ORD_DVSN": "01",
        "CMA_EVLU_AMT_ICLD_YN": "Y",
        "OVRS_ICLD_YN": "Y"
    }
    res = requests.get(URL, headers=headers, params=params)
    cash = res.json()['output']['ord_psbl_cash']
    send_message(f"주문 가능 현금 잔고: {cash}원", DISCORD_WEBHOOK_URL)
    return int(cash)

def buy(code, qty, APP_KEY, APP_SECRET, URL_BASE):
    """주식 시장가 매수"""
    ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
    PATH = "uapi/domestic-stock/v1/trading/order-cash"
    URL = f"{URL_BASE}/{PATH}"
    data = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "01",
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": "0",
    }
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "TTTC0802U",
        "custtype": "P",
        "hashkey": hashkey(data, APP_KEY, APP_SECRET, URL_BASE)
    }
    res = requests.post(URL, headers=headers, data=json.dumps(data))
    if res.json()['rt_cd'] == '0':
        send_message(f"[매수 성공]{str(res.json())}", DISCORD_WEBHOOK_URL)
        return True
    else:
        send_message(f"[매수 실패]{str(res.json())}", DISCORD_WEBHOOK_URL)
        return False

def sell(code, qty, APP_KEY, APP_SECRET, URL_BASE):
    """주식 시장가 매도"""
    ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
    PATH = "uapi/domestic-stock/v1/trading/order-cash"
    URL = f"{URL_BASE}/{PATH}"
    data = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "01",
        "ORD_QTY": qty,
        "ORD_UNPR": "0",
    }
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "TTTC0801U",
        "custtype": "P",
        "hashkey": hashkey(data, APP_KEY, APP_SECRET, URL_BASE)
    }
    res = requests.post(URL, headers=headers, data=json.dumps(data))
    if res.json()['rt_cd'] == '0':
        send_message(f"[매도 성공]{str(res.json())}", DISCORD_WEBHOOK_URL)
        return True
    else:
        send_message(f"[매도 실패]{str(res.json())}", DISCORD_WEBHOOK_URL)
        return False

# Streamlit UI
st.title('자동 주식 매매 프로그램')

# 사용자 정보 입력
APP_KEY = st.text_input('APP_KEY', value='')
APP_SECRET = st.text_input('APP_SECRET', value='', type='password')
CANO = st.text_input('CANO', value='')
ACNT_PRDT_CD = st.text_input('ACNT_PRDT_CD', value='')
DISCORD_WEBHOOK_URL = st.text_input('DISCORD_WEBHOOK_URL', value='')
URL_BASE = st.text_input('URL_BASE', value='https://openapi.koreainvestment.com:9443')

# 종목 코드 입력
stock_code = st.text_input('종목 코드', value='005930')

# 현재 주가 조회 버튼
if st.button('현재 주가 조회'):
    try:
        ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
        current_price, acml_vol = get_current_price_and_volume(stock_code, APP_KEY, APP_SECRET, URL_BASE)
        st.write(f'{stock_code}의 현재 주가는 {current_price}원입니다.')
    except Exception as e:
        st.error(f'주가 정보를 가져오는 데 오류가 발생했습니다: {e}')

# 현금 잔고 조회 버튼
if st.button('현금 잔고 조회'):
    try:
        ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
        cash_balance = get_balance(APP_KEY, APP_SECRET, URL_BASE)
        st.write(f'현재 현금 잔고는 {cash_balance}원입니다.')
    except Exception as e:
        st.error(f'현금 잔고를 가져오는 데 오류가 발생했습니다: {e}')

# 특정 주식 종목 데이터 조회 버튼
if st.button('종목 데이터 조회'):
    try:
        cursor.execute('SELECT * FROM price_info WHERE stock_code = ?', (stock_code,))
        rows = cursor.fetchall()
        if rows:
            st.write(f'{stock_code}의 데이터:')
            st.write(pd.DataFrame(rows, columns=['time_key', 'stock_code', 'high', 'low', 'open','close','volume']))
        else:
            st.write(f'{stock_code}에 대한 데이터가 없습니다.')
    except Exception as e:
        st.error(f'종목 데이터를 가져오는 데 오류가 발생했습니다: {e}')

# 자동매매 시작 버튼
if st.button('변동성 돌파전략 자동매매 시작'):
    try:
        ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
        total_cash = get_balance(APP_KEY, APP_SECRET, URL_BASE)
        bought = False
        bought_time = None
        buy_price = 0
        sell_price = 0
        total_profit = 0

        st.write('===국내 주식 자동매매 프로그램을 시작합니다===')
        send_message('===국내 주식 자동매매 프로그램을 시작합니다===', DISCORD_WEBHOOK_URL)

        profit_display = st.sidebar.empty()  # 수익률을 표시할 공간 예약

        while True:
            loop_start_time = datetime.datetime.now()

            t_now = datetime.datetime.now()
            t_start = t_now.replace(hour=9, minute=0, second=0, microsecond=0)
            t_sell = t_now.replace(hour=14, minute=50, second=0, microsecond=0)
            t_end = t_now.replace(hour=15, minute=0, second=0, microsecond=0)
            today = datetime.datetime.today().weekday()

            if today in [5, 6]:  # 토요일이나 일요일이면 자동 종료
                send_message("주말이므로 프로그램을 종료합니다.", DISCORD_WEBHOOK_URL)
                break

            if t_now >= t_end:
                send_message("오후 3시가 지났으므로 프로그램을 종료합니다.", DISCORD_WEBHOOK_URL)
                break

            current_price, current_volume = get_current_price_and_volume(stock_code, APP_KEY, APP_SECRET, URL_BASE)
            update_price_info(current_price, current_volume, t_now, stock_code)

            if t_start < t_now < t_sell and not bought:
                target_price = get_target_price_change(stock_code)

                if target_price and target_price <= current_price:
                    buy_qty = int(total_cash // current_price)
                    if buy_qty > 0:
                        result = buy(stock_code, buy_qty, APP_KEY, APP_SECRET, URL_BASE)
                        if result:
                            bought = True
                            buy_price = current_price
                            # 매도 시간을 현재 시간의 마지막 초로 설정
                            bought_time = t_now.replace(minute=59, second=59)
                            send_message(f"{stock_code} 매수 완료", DISCORD_WEBHOOK_URL)
                            st.write(f"{stock_code} 매수 완료")

            # 특정 시간의 마지막 초에 매도
            if bought and t_now >= bought_time:
                stock_dict = get_stock_balance(APP_KEY, APP_SECRET, URL_BASE)
                qty = stock_dict.get(stock_code, 0)
                if qty:
                    qty = int(qty)
                if qty > 0:
                    result = sell(stock_code, qty, APP_KEY, APP_SECRET, URL_BASE)
                    if result:
                        bought = False
                        sell_price = current_price
                        profit = ((sell_price - buy_price) / buy_price) * 100 - 0.2
                        total_profit += profit
                        send_message(f"{stock_code} 매도 완료", DISCORD_WEBHOOK_URL)
                        st.write(f"{stock_code} 매도 완료")

            if t_now >= t_sell and bought:
                stock_dict = get_stock_balance(APP_KEY, APP_SECRET, URL_BASE)
                qty = stock_dict.get(stock_code, 0)
                if qty:
                    qty = int(qty)
                if qty > 0:
                    sell(stock_code, qty, APP_KEY, APP_SECRET, URL_BASE)
                    bought = False
                    sell_price = current_price
                    profit = ((sell_price - buy_price) / buy_price) * 100 - 0.2
                    total_profit += profit
                    send_message(f"장 마감 강제 매도: {stock_code}", DISCORD_WEBHOOK_URL)
                    st.write(f"장 마감 강제 매도: {stock_code}")

            # 수익률 표시
            profit_display.write(f"오늘의 수익률: {total_profit:.2f}%")

            loop_end_time = datetime.datetime.now()
            elapsed_time = (loop_end_time - loop_start_time).total_seconds()
            sleep_time = max(5 - elapsed_time, 0)

            time.sleep(sleep_time)




    except Exception as e:
        send_message(f"[오류 발생]{e}", DISCORD_WEBHOOK_URL)
        st.error(f"오류 발생: {e}")

    finally:
        send_message("프로그램이 종료되었습니다.", DISCORD_WEBHOOK_URL)
        st.write("프로그램이 종료되었습니다.")


# 이동평균선 돌파전략 자동매매 시작 버튼
if st.button('이동평균선 돌파전략 자동매매 시작'):
    try:
        ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
        total_cash = get_balance(APP_KEY, APP_SECRET, URL_BASE)
        bought = False
        buy_price = 0
        sell_price = 0
        total_profit = 0

        st.write('===국내 주식 자동매매 프로그램을 시작합니다===')
        send_message('===국내 주식 자동매매 프로그램을 시작합니다===', DISCORD_WEBHOOK_URL)

        profit_display = st.sidebar.empty()  # 수익률을 표시할 공간 예약

        while True:
            loop_start_time = datetime.datetime.now()

            t_now = datetime.datetime.now()
            t_start = t_now.replace(hour=9, minute=0, second=0, microsecond=0)
            t_sell = t_now.replace(hour=14, minute=50, second=0, microsecond=0)
            t_end = t_now.replace(hour=15, minute=0, second=0, microsecond=0)
            today = datetime.datetime.today().weekday()

            if today in [5, 6]:  # 토요일이나 일요일이면 자동 종료
                send_message("주말이므로 프로그램을 종료합니다.", DISCORD_WEBHOOK_URL)
                break

            if t_now >= t_end:
                send_message("오후 3시가 지났으므로 프로그램을 종료합니다.", DISCORD_WEBHOOK_URL)
                break

            if t_now.hour >= 9:
                current_price, current_volume = get_current_price_and_volume(stock_code, APP_KEY, APP_SECRET, URL_BASE)
                update_price_info(current_price, current_volume, t_now, stock_code)

                df = get_target_price_ma(stock_code)
                if df['SMA2'][-1] > df['SMA4'][-1] and not bought:
                    buy_qty = int(total_cash // current_price)
                    if buy_qty > 0:
                        result = buy(stock_code, buy_qty, APP_KEY, APP_SECRET, URL_BASE)
                        if result:
                            bought = True
                            buy_price = current_price
                            
                            send_message(f"{stock_code} 매수 완료", DISCORD_WEBHOOK_URL)
                            st.write(f"{stock_code} 매수 완료")

                
                if bought and df['SMA2'][-1] < df['SMA4'][-1]:
                    stock_dict = get_stock_balance(APP_KEY, APP_SECRET, URL_BASE)
                    qty = stock_dict.get(stock_code, 0)
                    if qty:
                        qty = int(qty)
                    if qty > 0:
                        result = sell(stock_code, qty, APP_KEY, APP_SECRET, URL_BASE)
                        if result:
                            bought = False
                            sell_price = current_price
                            profit = ((sell_price - buy_price) / buy_price) * 100 - 0.2
                            total_profit += profit
                            send_message(f"{stock_code} 매도 완료", DISCORD_WEBHOOK_URL)
                            st.write(f"{stock_code} 매도 완료")

                if t_now >= t_sell and bought:
                    stock_dict = get_stock_balance(APP_KEY, APP_SECRET, URL_BASE)
                    qty = stock_dict.get(stock_code, 0)
                    if qty > 0:
                        sell(stock_code, qty, APP_KEY, APP_SECRET, URL_BASE)
                        bought = False
                        sell_price = current_price
                        profit = ((sell_price - buy_price) / buy_price) * 100 - 0.2
                        total_profit += profit
                        send_message(f"장 마감 강제 매도: {stock_code}", DISCORD_WEBHOOK_URL)
                        st.write(f"장 마감 강제 매도: {stock_code}")

                # 수익률 표시
                profit_display.write(f"오늘의 수익률: {total_profit:.2f}%")

            loop_end_time = datetime.datetime.now()
            elapsed_time = (loop_end_time - loop_start_time).total_seconds()
            sleep_time = max(5 - elapsed_time, 0)

            time.sleep(sleep_time)

    except Exception as e:
        send_message(f"[오류 발생]{e}", DISCORD_WEBHOOK_URL)
        st.error(f"오류 발생: {e}")

    finally:
        send_message("프로그램이 종료되었습니다.", DISCORD_WEBHOOK_URL)
        st.write("프로그램이 종료되었습니다.")

if st.button('변동성 돌파전략 + 모델1 자동매매 시작'):
    try:
        ensure_token_valid(APP_KEY, APP_SECRET, URL_BASE)
        total_cash = get_balance(APP_KEY, APP_SECRET, URL_BASE)
        bought = False
        bought_time = None
        buy_price = 0
        sell_price = 0
        total_profit = 0

        st.write('===국내 주식 자동매매 프로그램을 시작합니다===')
        send_message('===국내 주식 자동매매 프로그램을 시작합니다===', DISCORD_WEBHOOK_URL)
        
        profit_display = st.sidebar.empty()

        while True:
            loop_start_time = datetime.datetime.now()

            t_now = datetime.datetime.now()
            t_start = t_now.replace(hour=9, minute=0, second=0, microsecond=0)
            t_sell = t_now.replace(hour=14, minute=50, second=0, microsecond=0)
            t_end = t_now.replace(hour=15, minute=0, second=0, microsecond=0)
            today = datetime.datetime.today().weekday()

            if today in [5, 6]:  # 토요일이나 일요일이면 자동 종료
                send_message("주말이므로 프로그램을 종료합니다.", DISCORD_WEBHOOK_URL)
                break

            if t_now >= t_end:
                send_message("오후 3시가 지났으므로 프로그램을 종료합니다.", DISCORD_WEBHOOK_URL)
                break

            current_price, current_volume = get_current_price_and_volume(stock_code, APP_KEY, APP_SECRET, URL_BASE)
            update_price_info(current_price, current_volume, t_now, stock_code)

            current_hour_key = t_now.strftime('%Y-%m-%d %H')  # current_hour_key 할당

            if t_start < t_now < t_sell and not bought:
                target_price = get_target_price_change(stock_code)
                model_prediction = get_model_prediction(stock_code, current_hour_key)

                if target_price and target_price <= current_price and model_prediction == 1:
                    buy_qty = int(total_cash // current_price)
                    if buy_qty > 0:
                        result = buy(stock_code, buy_qty, APP_KEY, APP_SECRET, URL_BASE)
                        if result:
                            bought = True
                            buy_price = current_price
                            # 매도 시간을 현재 시간의 마지막 초로 설정
                            bought_time = t_now.replace(minute=59, second=59)
                            send_message(f"{stock_code} 매수 완료", DISCORD_WEBHOOK_URL)
                            st.write(f"{stock_code} 매수 완료")

            # 특정 시간의 마지막 초에 매도
            if bought and t_now >= bought_time:
                stock_dict = get_stock_balance(APP_KEY, APP_SECRET, URL_BASE)
                qty = stock_dict.get(stock_code, 0)
                if qty:
                    qty = int(qty)
                if qty > 0:
                    result = sell(stock_code, qty, APP_KEY, APP_SECRET, URL_BASE)
                    if result:
                        bought = False
                        sell_price = current_price
                        profit = ((sell_price - buy_price) / buy_price) * 100 - 0.2
                        total_profit += profit
                        send_message(f"{stock_code} 매도 완료", DISCORD_WEBHOOK_URL)
                        st.write(f"{stock_code} 매도 완료")

            if t_now >= t_sell and bought:
                stock_dict = get_stock_balance(APP_KEY, APP_SECRET, URL_BASE)
                qty = stock_dict.get(stock_code, 0)
                if qty > 0:
                    sell(stock_code, qty, APP_KEY, APP_SECRET, URL_BASE)
                    bought = False
                    sell_price = current_price
                    profit = ((sell_price - buy_price) / buy_price) * 100 - 0.2
                    total_profit += profit
                    send_message(f"장 마감 강제 매도: {stock_code}", DISCORD_WEBHOOK_URL)
                    st.write(f"장 마감 강제 매도: {stock_code}")

            # 수익률 표시
            profit_display.write(f"오늘의 수익률: {total_profit:.2f}%")

            loop_end_time = datetime.datetime.now()
            elapsed_time = (loop_end_time - loop_start_time).total_seconds()
            sleep_time = max(5 - elapsed_time, 0)

            time.sleep(sleep_time)

    except Exception as e:
        send_message(f"[오류 발생]{e}", DISCORD_WEBHOOK_URL)
        st.error(f"오류 발생: {e}")

    finally:
        send_message("프로그램이 종료되었습니다.", DISCORD_WEBHOOK_URL)
        st.write("프로그램이 종료되었습니다.")
