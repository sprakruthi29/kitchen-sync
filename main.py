from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import database

app = FastAPI(title="KitchenSync API")
database.init_db()

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_ui():
    return FileResponse("static/index.html")

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

class OrderItemInput(BaseModel):
    menu_item_id: int
    quantity: int
    notes: Optional[str] = ""

class OrderCreate(BaseModel):
    table_id: int
    guest_count: int = 2
    items: List[OrderItemInput]

class StatusUpdate(BaseModel):
    status: str

VALID_TRANSITIONS = {
    "QUEUED": ["PREPARING"],
    "PREPARING": ["READY"],
    "READY": ["SERVED"],
    "SERVED": []
}

@app.get("/api/state")
def get_full_state():
    conn = database.get_db()
    menu = [dict(r) for r in conn.execute("SELECT * FROM menu_items").fetchall()]
    tables = [dict(r) for r in conn.execute("SELECT * FROM restaurant_tables").fetchall()]
    
    orders_rows = conn.execute("""
        SELECT o.*, t.table_number 
        FROM orders o JOIN restaurant_tables t ON o.table_id = t.id
        WHERE o.status = 'ACTIVE' ORDER BY o.created_at ASC
    """).fetchall()
    
    orders = []
    now = datetime.utcnow()
    for o in orders_rows:
        o_dict = dict(o)
        created = datetime.fromisoformat(o_dict["created_at"])
        elapsed_mins = int((now - created).total_seconds() // 60)
        o_dict["elapsed_mins"] = elapsed_mins
        
        items = [dict(i) for i in conn.execute("""
            SELECT oi.*, m.name, m.station, m.prep_time_mins, m.allergens
            FROM order_items oi JOIN menu_items m ON oi.menu_item_id = m.id
            WHERE oi.order_id = ?
        """, (o_dict["id"],)).fetchall()]
        
        max_prep = max([i["prep_time_mins"] for i in items], default=10)
        o_dict["max_prep_mins"] = max_prep
        o_dict["is_delayed"] = elapsed_mins > (max_prep + 10)
        o_dict["items"] = items
        orders.append(o_dict)

    best_sellers = [dict(r) for r in conn.execute("""
        SELECT m.name, SUM(oi.quantity) as total_sold, SUM(oi.quantity * oi.unit_price_paise) as revenue_paise
        FROM order_items oi JOIN menu_items m ON oi.menu_item_id = m.id
        GROUP BY m.id ORDER BY total_sold DESC LIMIT 5
    """).fetchall()]

    conn.close()
    return {"menu": menu, "tables": tables, "orders": orders, "best_sellers": best_sellers}

@app.patch("/api/menu/{item_id}/toggle-stock")
async def toggle_stock(item_id: int):
    conn = database.get_db()
    row = conn.execute("SELECT is_available FROM menu_items WHERE id = ?", (item_id,)).fetchone()
    if row:
        new_status = 0 if row["is_available"] == 1 else 1
        conn.execute("UPDATE menu_items SET is_available = ? WHERE id = ?", (new_status, item_id))
        conn.commit()
    conn.close()
    await manager.broadcast({"event": "UPDATE"})
    return {"message": "Stock status toggled"}

@app.post("/api/orders")
async def create_order(order: OrderCreate):
    conn = database.get_db()
    existing = conn.execute("SELECT id FROM orders WHERE table_id = ? AND status = 'ACTIVE'", (order.table_id,)).fetchone()
    order_id = existing["id"] if existing else conn.execute(
        "INSERT INTO orders (table_id, created_at, status, guest_count) VALUES (?, ?, 'ACTIVE', ?)",
        (order.table_id, datetime.utcnow().isoformat(), order.guest_count)
    ).lastrowid

    for item in order.items:
        menu_row = conn.execute("SELECT price_paise FROM menu_items WHERE id = ?", (item.menu_item_id,)).fetchone()
        if not menu_row:
            continue
        conn.execute(
            "INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price_paise, notes, status) VALUES (?, ?, ?, ?, ?, 'QUEUED')",
            (order_id, item.menu_item_id, item.quantity, menu_row["price_paise"], item.notes)
        )

    conn.execute("UPDATE restaurant_tables SET status = 'OCCUPIED' WHERE id = ?", (order.table_id,))
    conn.commit()
    conn.close()
    await manager.broadcast({"event": "NEW_ORDER"})
    return {"order_id": order_id}

@app.patch("/api/order-items/{item_id}/status")
async def update_item_status(item_id: int, payload: StatusUpdate):
    conn = database.get_db()
    row = conn.execute("SELECT status, order_id FROM order_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Item not found")
    
    current_status = row["status"]
    if payload.status not in VALID_TRANSITIONS.get(current_status, []):
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid transition")

    conn.execute("UPDATE order_items SET status = ? WHERE id = ?", (payload.status, item_id))
    order_id = row["order_id"]
    
    statuses = [r["status"] for r in conn.execute("SELECT status FROM order_items WHERE order_id = ?", (order_id,)).fetchall()]
    table_id = conn.execute("SELECT table_id FROM orders WHERE id = ?", (order_id,)).fetchone()["table_id"]
    
    if all(s in ("READY", "SERVED") for s in statuses) and any(s == "READY" for s in statuses):
        conn.execute("UPDATE restaurant_tables SET status = 'READY_PICKUP' WHERE id = ?", (table_id,))
    
    conn.commit()
    conn.close()
    await manager.broadcast({"event": "UPDATE"})
    return {"status": payload.status}

@app.get("/api/tables/{table_id}/bill")
def generate_bill(table_id: int, split_by: int = 1):
    conn = database.get_db()
    order = conn.execute("SELECT id FROM orders WHERE table_id = ? AND status = 'ACTIVE'", (table_id,)).fetchone()
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="No active order")
    
    items = [dict(r) for r in conn.execute("""
        SELECT m.name, oi.quantity, oi.unit_price_paise, (oi.quantity * oi.unit_price_paise) as line_total_paise
        FROM order_items oi JOIN menu_items m ON oi.menu_item_id = m.id
        WHERE oi.order_id = ?
    """, (order["id"],)).fetchall()]
    conn.close()

    subtotal_paise = sum(i["line_total_paise"] for i in items)
    gst_paise = int(round(subtotal_paise * 0.05))
    grand_total_paise = subtotal_paise + gst_paise
    split_count = max(1, split_by)
    per_person_paise = grand_total_paise // split_count

    return {
        "subtotal": subtotal_paise / 100,
        "gst": gst_paise / 100,
        "grand_total": grand_total_paise / 100,
        "split_by": split_count,
        "per_person": per_person_paise / 100,
        "items": items
    }

@app.post("/api/tables/{table_id}/pay")
async def settle_bill(table_id: int):
    conn = database.get_db()
    conn.execute("UPDATE orders SET status = 'PAID' WHERE table_id = ? AND status = 'ACTIVE'", (table_id,))
    conn.execute("UPDATE restaurant_tables SET status = 'AVAILABLE' WHERE id = ?", (table_id,))
    conn.commit()
    conn.close()
    await manager.broadcast({"event": "UPDATE"})
    return {"message": "Paid"}