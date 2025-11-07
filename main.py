import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from bson import ObjectId

from database import db, create_document, get_documents

app = FastAPI(title="Vibe Commerce API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Utility helpers
# -----------------------------
class ObjectIdStr(str):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if isinstance(v, ObjectId):
            return str(v)
        if isinstance(v, str):
            return v
        raise TypeError("ObjectId or str required")


def serialize_doc(doc: dict) -> dict:
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    return d


# -----------------------------
# Schemas (specific to this app)
# -----------------------------
class ProductIn(BaseModel):
    name: str
    price: float = Field(ge=0)


class CartAddRequest(BaseModel):
    product_id: str
    qty: int = Field(ge=1)


class CartItemOut(BaseModel):
    id: Optional[ObjectIdStr] = None
    product_id: str
    name: str
    price: float
    qty: int
    line_total: float


class CartSummary(BaseModel):
    items: List[CartItemOut]
    total: float


# -----------------------------
# Mock product seed data
# -----------------------------
MOCK_PRODUCTS: List[ProductIn] = [
    ProductIn(name="Minimal Card Holder", price=19.0),
    ProductIn(name="Pastel Wallet", price=29.0),
    ProductIn(name="Matte Visa Card", price=9.0),
    ProductIn(name="E-commerce Tote", price=24.0),
    ProductIn(name="Fintech Hoodie", price=49.0),
    ProductIn(name="Soft Touch Notebook", price=14.0),
]


# -----------------------------
# Health + DB test
# -----------------------------
@app.get("/")
def read_root():
    return {"message": "Vibe Commerce Backend Running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name if hasattr(db, "name") else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    return response


# -----------------------------
# Product endpoints
# -----------------------------
@app.get("/api/product")
def get_products():
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    # Seed if empty
    count = db["product"].count_documents({})
    if count == 0:
        for p in MOCK_PRODUCTS:
            create_document("product", p.model_dump())

    products = list(db["product"].find({}))
    return [
        {"id": str(p["_id"]), "name": p.get("name"), "price": float(p.get("price", 0))}
        for p in products
    ]


# -----------------------------
# Cart endpoints
# -----------------------------
@app.get("/api/cart")
def get_cart() -> CartSummary:
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    items = list(db["cart"].find({}))
    # Build product lookup
    product_ids = list({ObjectId(i["product_id"]) for i in items}) if items else []
    product_map = {}
    if product_ids:
        for p in db["product"].find({"_id": {"$in": product_ids}}):
            product_map[str(p["_id"])] = p

    out_items: List[CartItemOut] = []
    total = 0.0
    for it in items:
        pid = str(it.get("product_id"))
        qty = int(it.get("qty", 1))
        p = product_map.get(pid)
        if not p:
            # Skip items referencing missing products
            continue
        price = float(p.get("price", 0))
        line_total = price * qty
        total += line_total
        out_items.append(
            CartItemOut(
                id=str(it.get("_id")),
                product_id=pid,
                name=p.get("name", ""),
                price=price,
                qty=qty,
                line_total=line_total,
            )
        )

    return CartSummary(items=out_items, total=round(total, 2))


@app.get("/api/cart/total")
def get_cart_total():
    summary = get_cart()
    return {"total": summary.total}


@app.post("/api/cart")
def add_to_cart(payload: CartAddRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    # Validate product exists
    try:
        prod = db["product"].find_one({"_id": ObjectId(payload.product_id)})
    except Exception:
        prod = None
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    # Upsert: if an entry already exists for this product, increment qty
    existing = db["cart"].find_one({"product_id": payload.product_id})
    if existing:
        new_qty = int(existing.get("qty", 1)) + payload.qty
        db["cart"].update_one({"_id": existing["_id"]}, {"$set": {"qty": new_qty, "updated_at": datetime.now(timezone.utc)}})
        cart_id = str(existing["_id"])
    else:
        cart_id = create_document(
            "cart",
            {
                "product_id": payload.product_id,
                "qty": payload.qty,
            },
        )

    return {"message": "Added to cart", "id": cart_id}


@app.delete("/api/cart/{cart_item_id}")
def remove_cart_item(cart_item_id: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        result = db["cart"].delete_one({"_id": ObjectId(cart_item_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cart item id")
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Cart item not found")
    return {"message": "Removed"}


@app.post("/api/checkout")
def checkout():
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    summary = get_cart()
    if not summary.items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    # Create a mock receipt record in 'order' collection
    receipt = {
        "items": [item.model_dump() for item in summary.items],
        "total": summary.total,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    order_id = create_document("order", receipt)

    # Clear the cart
    db["cart"].delete_many({})

    receipt["id"] = order_id
    return receipt


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
