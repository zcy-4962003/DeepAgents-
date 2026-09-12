"""项目入口：将 FastAPI 应用暴露给 uvicorn 启动。

运行方式：

    uvicorn main:app --reload --host 0.0.0.0 --port 8000

也可以直接：

    python main.py
"""

from app.api.server import app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
