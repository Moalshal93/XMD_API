import re

from scrapy import Selector
from curl_cffi import CurlMime, requests
from fastapi import FastAPI, Request
from datetime import datetime, timedelta
import logging
import traceback


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()

LOGIN_URL = "https://xbo.xyngular.com/Account/Login"
COSTUMERS_PAGE_URL = "https://xbo.xyngular.com/ReportCenter/new-customers-and-partners"
COSTUMERS_DATA_ENDPOINT = "https://xbo.xyngular.com/SqlGridServer/new-customers-and-partners"
SALES_ENDPOINT = "https://xbo.xyngular.com/WidgetsContainer/OrderHistory/{customerID}"
ORDER_ENDPOINT = "https://xbo.xyngular.com/Shopping/InvoiceRenderer/1/{orderID}?handler=LoadInvoice"

# ========================================
# Utility functions
# ========================================
def get_yesterday(days=1):
    return (datetime.now() - timedelta(days=days)).date()

def get_date_days_ago(days=7):
    days_list = []
    for day in range(1, days + 1):
        days_list.append((datetime.now() - timedelta(days=day)).date())
    return days_list

async def login(username: str, password: str, session: requests.AsyncSession):
    for attempt in range(3):
        try:
            r = await session.get(LOGIN_URL)
            if r.status_code != 200:
                return {"status": False, "Error": f"Failed to load login page"}
            
            main_page = await session.get(LOGIN_URL)
            sel = Selector(text=main_page.text)

            request_verification_token = sel.css('input[name="__RequestVerificationToken"]::attr(value)').get()

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                "x-requested-with": "XMLHttpRequest"
            }

            mp = CurlMime()
            mp.addpart(name="UserData.UserName", data=username.encode())
            mp.addpart(name="UserData.Password", data=password.encode())
            mp.addpart(name="__RequestVerificationToken", data=request_verification_token)
            mp.addpart(name="X-Requested-With", data=b"XMLHttpRequest")

            login_response = await session.post(LOGIN_URL, multipart=mp, headers=headers)
            mp.close()

            if '"Success":false' in login_response.text:
                return {"status": False, "Error": "Invalid credentials"}
            
            return {"status": True}
        
        except:
            logging.exception("Login attempt failed")
            logging.error(f"Attempt {attempt + 1} failed with error: {traceback.format_exc()}")
            print(f"Attempt {attempt + 1} failed with error: {traceback.format_exc()}")
            if attempt == 2:
                return {"status": False, "Error": "Login failed after 3 attempts"}


# ========================================
# Customers API functions
# ========================================
async def extract_customers_details(session, customers_rows):
    
    headers = {
        "x-requested-with": "XMLHttpRequest"        
    }
    
    customers = []
    for row in customers_rows:  
        
        customer_id = Selector(text=row[1]).css("a::text").get()
        
        sales_response = await session.get(SALES_ENDPOINT.format(customerID=customer_id), headers=headers)
        sales_sel = Selector(text=sales_response.text)
        sales_rows = sales_sel.xpath("//div[contains(@id, 'OrderHistory-Orders')]//tr[@data-id and @data-level]")
        
        sales_total = 0.0
        items_list = []
        
        for idx, sales_row in enumerate(sales_rows):
            order_id = sales_row.xpath(".//td[1]/a/text()").get()
            order_total = sales_row.xpath(".//td[2]/text()").get()
            if order_total:
                order_total_cleaned = float(order_total.replace("$", ""))
                sales_total += order_total_cleaned
            
            order_response = await session.get(ORDER_ENDPOINT.format(orderID=order_id), headers=headers)
            order_sel = Selector(text=order_response.text)
            
            if idx == 0:  # Only extract address details from the first order
                address_street = order_sel.xpath("//table[@class='table-info'][1]//tr[3]//td[2]/text()").get()
                address_postal = order_sel.xpath("//table[@class='table-info'][1]//tr[5]//td[2]/text()").get()

                match = re.match(r'^(.+?)\s*,\s*([A-Z]{2})\s+(\d{5})$', address_postal)
                if match:
                    city, state, postal = match.groups()
                    
                else:
                    city, state, postal = "", "", ""
                
            itemID = order_sel.xpath("//table[@class='table-section table-bordered' and contains(., 'ItemID')]//tbody//tr")
            itemID_texts = [tr.xpath(".//td[1]/text()").get() for tr in itemID]
            items_list.extend(itemID_texts)
                
        customers.append({
            "First Name": row[2].split()[0] if row[2].split() else "",
            "Last Name": row[2].split()[-1] if len(row[2].split()) > 1 else "",
            "Account Type": row[3],
            "Level": row[4],
            "Phone": row[6],
            "Email": row[7],
            "Sponser Name": Selector(text=row[12]).css("a::text").get(),
            "Frontline Lead": row[13],
            "Entry Channel": row[14],
            "Total": f"${sales_total:.2f}",
            "Street": address_street.strip() if address_street else "",
            "City": city,
            "State": state,
            "Postal": postal,
            "ItemID": ", ".join(items_list)
        })

    return customers


async def get_new_customers(session: requests.AsyncSession, days=None):
    if not days:
        days = [get_yesterday()]
    else:
        days = get_date_days_ago(days)

    
    res = await session.get(COSTUMERS_PAGE_URL)
    
    if res.status_code != 200:
        return {"status": False, "Error": f"Fetch failed customers page (error {res.status_code})"}
    
    sel = Selector(text=res.text)

    periodIDs_options = sel.css('select#ReportPeriodID option')
    
    if not periodIDs_options:
        return {"status": True, "customers": []}
    
    periodIDs = [{"month": option.css('::text').get(), "value": option.css('::attr(value)').get()} for option in periodIDs_options]

    first_period_id = periodIDs[0]["value"] if periodIDs else None
    second_period_id = periodIDs[1]["value"] if len(periodIDs) > 1 else None
    
    if any(day for day in days if day.day == 1):
        target_periods = [first_period_id, second_period_id]
    else:
        target_periods = [first_period_id]

    customers_data = []

    request_verification_token = sel.css('input[name="__RequestVerificationToken"]::attr(value)').get()

    for idx, period_id in enumerate(target_periods):

        headers = {
            "content-type": "application/json",
            "x-requested-with": "XMLHttpRequest",
            "x-xsrf-token": request_verification_token
            
        }

        data = {"GridParams":{"CurrentPeriodID":period_id,"PeriodID":period_id,"CommissionPeriods":"[]","LevelStart":None,"LevelEnd":None,"AccountNames":"[]","CustomerTypes":"[]","PersonalVolumeStart":None,"PersonalVolumeStartEnd":None,"CurrentLifetimeRanks":"[]","CurrentSponsors":"[]","States":"[]","LifetimeRanks":"[]","PaidRanks":"[]","BirthdayMonth":"[]","DaysAsPartnerStart":None,"DaysAsPartnerEnd":None,"PreviousSponsors":"[]","AccountNamesInc":"[]","CurrentSponsorsInc":"[]","StoreAllRowsInCache":False},"draw":idx+1,"columns":[{"data":0,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":1,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":2,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":3,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":4,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":5,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":6,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":7,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":8,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":9,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":10,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":11,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":12,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":13,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}},{"data":14,"name":"","searchable":False,"orderable":True,"search":{"value":"","regex":False}}],"order":[],"start":0,"length":100,"search":{"value":"","regex":False}}

        res = await session.post(COSTUMERS_DATA_ENDPOINT, headers=headers, json=data, timeout=60)
        
        data_ = res.json().get("data", [])
        # print(f"Fetched {len(data_)} customers for period ID {period_id}")
        
        customers_data.extend(data_)
    
    if not customers_data:
        return {"status": True, "customers": []}
    
    target_customers = []
    
    for customer in customers_data:
        if customer[5]:
            enrollment_date = datetime.strptime(customer[5], "%Y-%m-%d").date()
            if enrollment_date in days:
                target_customers.append(customer)

    if not target_customers:
        return {"status": True, "customers": []}
    
    refined_customers_data = await extract_customers_details(session, target_customers)

    return {"status": True, "customers": refined_customers_data}


@app.post("/newcustomer")
async def fetch_customers(request: Request):
    body = await request.json()
    username = body.get("username")
    password = body.get("password")
    days = body.get("days", 1)
    async with requests.AsyncSession(timeout=200, impersonate="chrome120") as session:
        try:
            logged = await login(username, password, session)
            if not logged["status"]:
                return {"Error": logged["Error"]}

            result = await get_new_customers(session, days=days)
            if not result["status"]:
                return {"Error": result["Error"]}

            return {
                "total": len(result["customers"]),
                "customers": result["customers"]
            }

        except Exception as e:
            logging.exception("Error while fetching customers")
            logging.error(f"Error details: {traceback.format_exc()}")
            print(f"Error details: {traceback.format_exc()}")
            return {"Error": str(e)}
        

      
@app.get("/")
def root():
    return {"message": "XMD API is running"}



# if __name__ == "__main__":
#     from fastapi.testclient import TestClient
    
#     client = TestClient(app)
#     response = client.post("/newcustomer", json={
#         "username": "peytonclark1997@gmail.com",
#         "password": "Eva2022!",
#         "days": 1
#     })
#     # print(response.json())
#     import json
#     with open("customers.json", "w") as f:
#         json.dump(response.json(), f, indent=4)

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
    
    
# curl -X POST "https://nmpvyora.onrender.com/newcustomer" ^
#   -d "{\"username\":\"sydney@vyorawellness.com\",\"password\":\"Vyora123!\",\"days\":1}"


# curl -X POST "http://127.0.0.1:8000/newcustomer" ^
#   -d "{\"username\":\"sydney@vyorawellness.com\",\"password\":\"Vyora123!\",\"days\":2}"


# URL: https://nmpvyora.onrender.com/newcustomer

# Body:
# {
#   "username": "sydney@vyorawellness.com",
#   "password": "Vyora123!",
#   "days": 2
# }

# Response:
# {
#   "total": 2,
#   "customers": [
#     {
#       "First Name": "Christy",
#       "Last Name": "Rockwood",
#       "First Purchase": "2026-06-02",
#       "Email": "christyrockwood@comcast.net",
#       "Source": "julieehenderson@ymail.com",
#       "LTV": "$359.10"
#     },
#     {
#       "First Name": "Letisha",
#       "Last Name": "Seyller",
#       "First Purchase": "2026-06-02",
#       "Email": "letish.seyller@gmail.com",
#       "Source": "danielle.s.kramer@gmail.com",
#       "LTV": "$359.10"
#     }
#   ]
# }