"""python -m local_app: intentionally loopback-only, one worker."""
import logging
import uvicorn

if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    uvicorn.run('local_app.api:create_app', factory=True, host='127.0.0.1', port=8765,
        workers=1, access_log=False)
