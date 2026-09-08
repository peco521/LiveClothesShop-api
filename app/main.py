from fastapi import FastAPI

app = FastAPI(
    title="LiveClothesShop API",
    version="1.0.0"
)


@app.get("/")
def root():
    return {
        "message": "LiveClothesShop API funcionando"
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }
