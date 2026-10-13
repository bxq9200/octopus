import asyncio
import uvicorn
from app.main import app

async def start():
    config = uvicorn.Config(app, host='0.0.0.0', port=8000, log_level='info', access_log=False)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == '__main__':
    asyncio.run(start())
