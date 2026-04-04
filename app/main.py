import uvicorn
from .web import create_app
from .config import AppConfig
def main():
    cfg = AppConfig.load('config/noise_warden.yaml')
    app = create_app('config/noise_warden.yaml')
    uvicorn.run(app, host=cfg['app']['host'], port=cfg['app']['port'])
if __name__ == '__main__': main()
