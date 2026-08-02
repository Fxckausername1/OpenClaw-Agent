import importlib
import importlib.metadata as md
packages = [
    ('pandas','pandas'),('numpy','numpy'),('scipy','scipy'),('matplotlib','matplotlib'),('seaborn','seaborn'),
    ('jupyterlab','jupyterlab'),('yfinance','yfinance'),('yahooquery','yahooquery'),('requests','requests'),
    ('bs4','beautifulsoup4'),('py_vollib','py_vollib'),('mibian','mibian'),('backtrader','backtrader'),
    ('vectorbt','vectorbt'),('numba','numba'),('sklearn','scikit-learn'),('statsmodels','statsmodels'),
    ('pandas_ta','pandas_ta'),('mplfinance','mplfinance'),('plotly','plotly'),('dash','dash'),
    ('jupyterlab_git','jupyterlab-git'),('nbdime','nbdime')
]

for mod, dist in packages:
    try:
        module = importlib.import_module(mod)
        try:
            ver = getattr(module,'__version__')
        except Exception:
            try:
                ver = md.version(dist)
            except Exception:
                ver = 'unknown'
        print(f"{mod}: OK, version={ver}")
    except Exception as e:
        print(f"{mod}: FAILED -> {e}")

# quick mini-check: compute a small option price using py_vollib BlackScholes
try:
    from py_vollib.black_scholes import black_scholes
    price = black_scholes('c', 100, 100, 30/365, 0.2, 0)
    print(f"py_vollib calc OK: {price}")
except Exception as e:
    print(f"py_vollib calc FAILED -> {e}")
