import os


def main() -> None:
    """Run the webhook server: `uv run lineback` (HOST / PORT env vars override defaults)."""
    import uvicorn

    uvicorn.run(
        "lineback.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
