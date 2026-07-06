# ML & Quantitative Trading Strategies Research Report
**Date: 2026-03-30 | Focus: Indian Markets (NSE/BSE) + Global Best Practices**

---

## Table of Contents
1. [Random Forest / XGBoost for Price Direction](#1-random-forest--xgboost-for-price-direction-prediction)
2. [LSTM / Neural Networks for Intraday](#2-lstm--neural-network-strategies-for-intraday)
3. [Reinforcement Learning Trading Agents](#3-reinforcement-learning-trading-agents)
4. [Feature Engineering for Stock Prediction](#4-feature-engineering-for-stock-prediction)
5. [Regime Detection Using ML](#5-regime-detection-using-ml-hmm-clustering)
6. [Sentiment Analysis Strategies](#6-sentiment-analysis-strategies-news-based-trading)
7. [Open-Source Quant Frameworks](#7-popular-open-source-quant-frameworks)
8. [India-Specific Resources](#8-india-specific-resources--angel-one-integration)
9. [Critical Practical Considerations](#9-critical-practical-considerations)
10. [Recommended Path for AutoTheta](#10-recommended-path-for-autotheta)

---

## 1. Random Forest / XGBoost for Price Direction Prediction

### Approach & Methodology
- **Classification problem**: Predict next-bar direction (up/down) rather than exact price
- **XGBoost** is the dominant choice due to built-in regularization (L1/L2), handling of missing values, and speed
- **LightGBM** is faster for large datasets; **CatBoost** handles categorical features natively
- Typical pipeline: Feature engineering -> Feature selection (top 20-30 indicators) -> Train/test split (walk-forward) -> Predict direction -> Generate signals

### Features Typically Used
- Technical indicators: RSI, MACD, Bollinger Bands, ATR, ADX, OBV, MFI
- Price-derived: Returns (1d, 5d, 10d, 20d), volatility (rolling std), momentum
- Volume: VWAP deviation, volume ratio, OBV slope
- Market microstructure: Bid-ask spread, order imbalance (if available)
- Best results with 20-30 features (more than 30 reduces performance)

### Backtested Results
- **Typical accuracy**: 52-58% direction prediction (anything above 55% with proper validation is strong)
- **Academic claims**: Up to 70% accuracy, but often with lookahead bias or in-sample testing
- **Realistic expectation**: 53-56% accuracy on unseen data, which can be profitable with proper risk management
- One optimized XGBoost model achieved higher returns than benchmark with lower volatility and max drawdown

### Key GitHub Repos
| Repo | Stars | Description |
|------|-------|-------------|
| [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) | 16.9k | Comprehensive book code: RF, XGBoost, LightGBM, CatBoost for trading |
| [gammarinaldi/ml-trading-random-forest-xgboost](https://github.com/gammarinaldi/ml-trading-random-forest-xgboost) | ~100 | Price prediction with RF + XGBoost, 32 technical indicators, ATR-based stops |
| [jiewwantan/XGBoost_stock_prediction](https://github.com/jiewwantan/XGBoost_stock_prediction) | ~200 | XGBoost daily direction classification with technical indicators |
| [microsoft/qlib](https://github.com/microsoft/qlib) | 39.7k | Microsoft's AI quant platform with LightGBM Alpha158 (17.83% annualized, IR 1.997) |

### Realistic Profitability Assessment
**MODERATE-TO-GOOD** for direction prediction if:
- Walk-forward validation is used (no lookahead)
- Transaction costs are included (0.03-0.05% per trade for Indian markets)
- Feature engineering is domain-driven, not data-mined
- Model is retrained periodically (weekly/monthly)
- Combined with proper position sizing and risk management

**WARNING**: Most academic papers showing 65%+ accuracy have methodological flaws (lookahead bias, in-sample testing, no transaction costs).

---

## 2. LSTM / Neural Network Strategies for Intraday

### Approach & Methodology
- **LSTM (Long Short-Term Memory)**: Sequence model that captures temporal dependencies in price data
- **Input**: Windowed sequences of features (e.g., last 60 bars of OHLCV + indicators)
- **Output**: Next-bar price, direction, or return magnitude
- **Architecture**: Typically 1-3 LSTM layers (64-256 units) + Dense output layer
- **Variants**: BiLSTM, Attention-LSTM, CNN-LSTM hybrids, Transformer models

### Common Features
- OHLCV data (normalized/differenced)
- Technical indicators as additional channels
- Time features (hour of day, day of week)
- Lagged returns at multiple horizons

### Backtested Results
- **Research claims**: RMSE improvements of 15-30% vs naive baselines
- **Trading results**: Mixed. Some show cumulative returns of 20-50% in backtests, but live trading results rarely match
- **FinBERT-LSTM hybrid**: 526% cumulative return with Sharpe 0.407 in one backtest (likely overfitted)
- **Realistic expectation**: Marginal improvement over simpler models, high computational cost

### Key GitHub Repos
| Repo | Stars | Description |
|------|-------|-------------|
| [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) | 16.9k | RNN/LSTM chapters with multivariate time series |
| [Hupperich-Manuel/LSTM-XGBoost-Hybrid-Forecasting](https://github.com/Hupperich-Manuel/LSTM-XGBoost-Hybrid-Forecasting) | ~50 | Hybrid LSTM-XGBoost for time series forecasting |
| [yashpandey474/Algorithmic-Trading-Model-BTC-USD](https://github.com/yashpandey474/Algorthmic-Trading-Model-for-BTC-USD-Crypto-Market) | ~30 | LSTM for BTC/USD with comprehensive backtesting |
| [jumbokh/NeuralNetworkStocks](https://github.com/jumbokh/NeuralNetworkStocks) | ~300 | Python + Keras stock predictions |

### Realistic Profitability Assessment
**POOR-TO-MODERATE** for direct price prediction:
- LSTMs are prone to **overfitting** on financial data due to low signal-to-noise ratio
- They struggle with **regime changes** and **black swan events**
- Prediction accuracy degrades rapidly over time without retraining
- High computational cost for marginal improvement over XGBoost
- **Better use case**: As a component in an ensemble, or for volatility prediction rather than price direction

### Why LSTMs Often Fail in Live Trading
1. Stock prices have very low signal-to-noise ratio
2. Non-stationary data distribution (what worked last year may not work now)
3. Models memorize patterns that don't persist
4. Cannot handle sudden macro/geopolitical shocks
5. Kaggle-style "LSTM predicts stock prices" tutorials are misleading (they predict lagged prices, not future prices)

---

## 3. Reinforcement Learning Trading Agents

### Approach & Methodology
- Agent learns optimal trading policy (buy/hold/sell) through interaction with market environment
- **State**: Current portfolio + market features (prices, indicators, positions)
- **Action**: Buy/sell/hold with position sizing
- **Reward**: Change in portfolio value, risk-adjusted returns, or Sharpe ratio
- **Algorithms**: PPO (most popular), A2C, DDPG, SAC, TD3
- Uses OpenAI Gym-style environments for training

### Key Framework: FinRL
- **14.6k stars** on GitHub
- Three-layer architecture: Market Environments -> DRL Agents -> Trading Applications
- Supports A2C, DDPG, PPO, SAC, TD3 via Stable Baselines 3
- Data from Yahoo Finance, Alpaca, Binance, and more

### Backtested Results (FinRL)
- **PPO on DOW 30**: Sharpe ratio 2.04, annual return 36.17%
- **DDPG on DOW 30**: Sharpe ratio 2.21, annual return 36.01%
- **FinRL-Podracer**: Cumulative returns ~111.5%, Sharpe ratios 2.12-2.42
- **Important caveat**: These are backtested on favorable periods; live results will differ significantly

### Key GitHub Repos
| Repo | Stars | Description |
|------|-------|-------------|
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 14.6k | Premier RL trading framework, 5 DRL agents, Stable Baselines 3 |
| [ebrahimpichka/DeepRL-trade](https://github.com/ebrahimpichka/DeepRL-trade) | ~300 | PPO and DQN trading agents |
| [Albert-Z-Guo/Deep-Reinforcement-Stock-Trading](https://github.com/Albert-Z-Guo/Deep-Reinforcement-Stock-Trading) | ~800 | Lightweight DRL framework for portfolio management |
| [MehranTaghian/DQN-Trading](https://github.com/MehranTaghian/DQN-Trading) | ~500 | DQN with CNN, CNN-GRU, and attention encoders |
| [theanh97/Deep-RL-Stock-Trading](https://github.com/theanh97/Deep-Reinforcement-Learning-with-Stock-Trading) | ~50 | PPO, A2C, DDPG, SAC, TD3 with transaction costs |

### Realistic Profitability Assessment
**POOR for retail traders, MODERATE for research**:
- RL agents are extremely sensitive to reward shaping and hyperparameters
- Training is unstable and non-reproducible
- Massive overfitting risk (agent memorizes training data patterns)
- Sim-to-real gap is enormous in financial markets
- The impressive backtested numbers (Sharpe > 2) almost never translate to live trading
- **Best use**: Research tool, understanding market dynamics, NOT for direct deployment

---

## 4. Feature Engineering for Stock Prediction

### Most Effective Feature Categories

#### A. Price-Based Features
| Feature | Description | Importance |
|---------|-------------|------------|
| Log returns (1d, 5d, 10d, 20d) | Multi-horizon momentum | HIGH |
| Rolling volatility (5d, 20d) | Regime indicator | HIGH |
| Price vs SMA (20, 50, 200) | Trend position | HIGH |
| RSI (14) | Overbought/oversold | MODERATE |
| MACD signal/histogram | Trend momentum | MODERATE |
| Bollinger Band %B | Volatility position | MODERATE |
| ATR (14) | Volatility magnitude | HIGH |
| ADX (14) | Trend strength | MODERATE |

#### B. Volume-Based Features
| Feature | Description | Importance |
|---------|-------------|------------|
| VWAP deviation | Institutional activity | HIGH |
| Volume ratio (current/avg) | Participation | HIGH |
| On-Balance Volume (OBV) slope | Accumulation/distribution | MODERATE |
| Money Flow Index (MFI) | Volume-weighted RSI | MODERATE |
| Volume-weighted returns | Smart money tracking | HIGH |

#### C. Market Microstructure
| Feature | Description | Importance |
|---------|-------------|------------|
| Bid-ask spread | Liquidity | HIGH (if available) |
| Order imbalance | Supply/demand | HIGH (if available) |
| Intraday gap % | Overnight sentiment | MODERATE |
| Open-to-high/low ratio | Intraday momentum | MODERATE |

#### D. Cross-Asset / Macro Features
| Feature | Description | Importance |
|---------|-------------|------------|
| VIX / India VIX | Fear gauge | HIGH |
| Sector relative strength | Rotation signal | MODERATE |
| FII/DII flows | Institutional sentiment | HIGH (India-specific) |
| USD/INR movement | Currency impact | MODERATE |
| US market overnight return | Global cue | HIGH |

### Feature Engineering Best Practices
1. **Avoid lookahead bias**: Use rolling windows, never full-period statistics
2. **Stationarity**: Use returns/differences, not raw prices
3. **Normalization**: Z-score with rolling window (not global normalization)
4. **Feature selection**: Mutual Information and Random Forest importance outperform correlation
5. **Optimal count**: 20-30 features; more than 30 reduces classification performance
6. **PCA**: Best feature extraction method for dimensionality reduction
7. **Domain expertise**: Features driven by trading hypotheses outperform data-mined features

### Key Reference
- [The Alpha Scientist - Feature Engineering](https://alphascientist.com/feature_engineering.html)
- [Springer Survey of Feature Selection](https://link.springer.com/article/10.1186/s40854-022-00441-7)

---

## 5. Regime Detection Using ML (HMM, Clustering)

### Approach & Methodology

#### Hidden Markov Models (HMM)
- Models market as switching between hidden states (bull/bear/sideways/high-vol)
- Trained on daily returns to identify 2-4 hidden states
- Each state has its own mean return and volatility distribution
- Library: `hmmlearn` in Python
- **Best for**: Detecting when market character changes, adapting strategy parameters

#### Gaussian Mixture Models (GMM)
- Clusters return distributions into regimes
- More flexible than HMM (no temporal ordering assumption)
- Often used alongside HMM for validation

#### K-Means Clustering
- Clusters multi-feature market snapshots into regimes
- Features: returns, volatility, volume, correlation, etc.
- Simpler but less theoretically grounded for time series

### Typical Regimes Detected
1. **Low-volatility bull** (trending up, small moves)
2. **High-volatility bull** (trending up, large moves)
3. **Low-volatility bear/sideways** (range-bound)
4. **High-volatility bear/crash** (trending down, large moves)

### Backtested Results
- **HMM 3-state model**: Sharpe ratio 1.9 on top-10 market cap stocks
- **Regime filter**: Reduced max drawdown from 56% to 24% (significant risk reduction)
- **HMM + Moving Average**: Outperformed buy-and-hold in total and risk-adjusted returns
- **Regime-adaptive strategy switching**: Outperforms any single static strategy

### Key GitHub Repos
| Repo | Stars | Description |
|------|-------|-------------|
| [taylorjmellon/market-regime-detection](https://github.com/taylorjmellon/market-regime-detection) | ~50 | K-Means + HMM for bull/bear/neutral detection |
| [Sakeeb91/market-regime-detection](https://github.com/Sakeeb91/market-regime-detection) | ~100 | HMM + ML for adaptive trading strategies |
| [theo-dim/regime_detection_ml](https://github.com/theo-dim/regime_detection_ml) | ~100 | HMM + SVM for regime detection |
| [yvesdhondt/MarketMoodRing](https://github.com/yvesdhondt/MarketMoodRing) | ~30 | HMM + Wasserstein K-Means for portfolio optimization |
| [Sakeeb91/regime-detection-strategy](https://github.com/Sakeeb91/regime-detection-strategy) | ~50 | Full pipeline: GMM/HMM + trend/mean-reversion/volatility strategies |

### Realistic Profitability Assessment
**GOOD -- Most practical ML approach for trading**:
- Regime detection is the MOST practically useful ML technique for trading
- Does not try to predict price (hard); instead detects market character (easier)
- Natural fit with your existing AutoTheta regime system (BULL/BEAR/CRASH)
- Reduces drawdowns significantly even if returns don't increase
- Can be combined with rule-based strategies (use ML for "when", rules for "what")
- **Recommended for AutoTheta**: Upgrade your current regime detection with HMM

---

## 6. Sentiment Analysis Strategies (News-Based Trading)

### Approach & Methodology
- **Pipeline**: Collect news/social media -> Extract sentiment -> Generate signals -> Trade
- **Models**: VADER (rule-based), TextBlob (simple NLP), FinBERT (BERT fine-tuned on financial text), LLaMA-2 (newest, best performance)
- **Signal**: Aggregate sentiment score -> threshold-based buy/sell signals

### Model Comparison
| Model | Accuracy | Speed | Use Case |
|-------|----------|-------|----------|
| VADER | ~56% | Very fast | Quick baseline, social media |
| TextBlob | ~55% | Fast | Simple NLP tasks |
| FinBERT | ~65-81% | Moderate | Financial news (purpose-built) |
| LLaMA-2 (fine-tuned) | Best | Slow | Superior but resource-heavy |

### Backtested Results
- **FinBERT strategy**: Average annual buy-and-hold return of 5.98%
- **LLaMA-2 strategy**: Average annual return of 12.37% (outperforms FinBERT)
- **FinBERT-LSTM hybrid**: Strong in backtests but fades under turbulent conditions
- **Key finding**: Sentiment signals work best as CONFIRMING indicators, not standalone signals

### Key GitHub Repos
| Repo | Stars | Description |
|------|-------|-------------|
| [ProsusAI/finBERT](https://github.com/ProsusAI/finBERT) | ~3k | Financial Sentiment Analysis with BERT |
| [tatsath/fin-ml](https://github.com/tatsath/fin-ml) | ~1k | NLP & Sentiment trading strategy case study |
| [Doj-i/The-NLP-News-Sentiment-Trading-Strategy](https://github.com/Doj-i/The-NLP-News-Sentiment-Trading-Strategy) | ~50 | VADER vs FinBERT comparison for S&P 500 |
| [digriffiths/Sentiment_Analysis_Based_Trading_Strategy](https://github.com/digriffiths/Sentiment_Analysis_Based_Trading_Strategy) | ~30 | VADER + TextBlob trading signals |

### Realistic Profitability Assessment
**POOR as standalone, MODERATE as supplement**:
- Standalone sentiment strategies produce modest returns (5-12% annually)
- Sentiment signals are most useful for:
  - Filtering trades (avoid trading against strong negative sentiment)
  - Event detection (earnings surprises, regulatory news)
  - Regime confirmation (broad market fear/greed)
- **Challenge for Indian markets**: English-language NLP works for global news, but Indian financial news quality and availability varies
- **Practical advice**: Use FinBERT as a filter/confirmation, not primary signal

---

## 7. Popular Open-Source Quant Frameworks

### Tier 1: Production-Ready Frameworks

| Framework | Stars | Language | Best For | Live Trading |
|-----------|-------|----------|----------|--------------|
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 48k | Python | Crypto trading bots | Yes |
| [Qlib (Microsoft)](https://github.com/microsoft/qlib) | 39.7k | Python | AI/ML quant research | No (research) |
| [Backtrader](https://github.com/mementum/backtrader) | 21k | Python | Strategy backtesting | Yes (IB, Oanda) |
| [Zipline](https://github.com/quantopian/zipline) | 20k | Python | Event-driven backtesting | No (archived) |
| [Lean (QuantConnect)](https://github.com/QuantConnect/Lean) | 18k | C#/Python | Multi-asset backtesting + live | Yes |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | 9.1k | Rust/Python | HFT, low-latency trading | Yes |
| [stefan-jansen/ML-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) | 16.9k | Python | ML trading education | No (education) |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 14.6k | Python | RL-based trading research | No (research) |

### Tier 2: Specialized Tools

| Framework | Stars | Language | Best For |
|-----------|-------|----------|----------|
| [PyBroker](https://github.com/edtechre/pybroker) | 3.3k | Python | ML-integrated backtesting |
| [VectorBT](https://github.com/polakowo/vectorbt) | 5k+ | Python | Fast vectorized backtesting |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 18k | Python | Market making, crypto |
| [vnpy](https://github.com/vnpy/vnpy) | 5k+ | Python | Full-featured (Chinese markets) |
| [QUANTAXIS](https://github.com/QUANTAXIS/QUANTAXIS) | 8k+ | Python | Chinese market focus |

### Framework Comparison for Your Use Case (Indian Options Trading)

| Criterion | Backtrader | PyBroker | Freqtrade | NautilusTrader |
|-----------|------------|----------|-----------|----------------|
| Options support | Limited | No | No | No |
| Indian broker integration | DIY | DIY | No | No |
| ML integration | Moderate | Excellent | Good (FreqAI) | Good |
| Ease of use | Good | Good | Excellent | Complex |
| Backtesting speed | Moderate | Fast (Numba) | Fast | Very fast (Rust) |
| Community | Large | Growing | Very large | Growing |
| **Recommendation for AutoTheta** | **Good** | **Best** | No | Overkill |

### Key Features of Top Frameworks

**Freqtrade (48k stars)**:
- FreqAI module for ML-based adaptive strategy optimization
- Self-training models that adapt to market conditions
- Telegram/WebUI management
- Extensive strategy marketplace
- Crypto-only (not suitable for Indian equity/options)

**Microsoft Qlib (39.7k stars)**:
- LightGBM Alpha158: 17.83% annualized return, IR 1.997, max DD -8.18%
- Transformer, LSTM, and ensemble models built-in
- Auto factor mining with LLM (RD-Agent)
- Chinese market focus but extensible
- Best-in-class for ML model benchmarking

**NautilusTrader (9.1k stars)**:
- Rust core, Python strategy layer
- Nanosecond-resolution backtesting
- 5 million rows/second streaming
- Fast enough to train RL agents
- Overkill for daily/hourly strategies but excellent for HFT

---

## 8. India-Specific Resources & Angel One Integration

### Angel One SmartAPI
| Repo | Description |
|------|-------------|
| [angel-one/smartapi-python](https://github.com/angel-one/smartapi-python) | Official Python SDK for Angel One |
| [ANANDAPADMANABHA/Trade-master](https://github.com/ANANDAPADMANABHA/Trade-master) | Advanced algo bot for Angel One |
| [itsvikask4/algorithmic-trading](https://github.com/itsvikask4/algorithmic-trading) | ORB strategy with SmartAPI |
| [NishchayShakya1/Algo_Trading](https://github.com/NishchayShakya1/Algo_Trading) | Multi-strategy system for Angel Broking |

### Indian Market Algo Trading
| Repo | Stars | Description |
|------|-------|-------------|
| [buzzsubash/algo_trading_strategies_india](https://github.com/buzzsubash/algo_trading_strategies_india) | 35 | NIFTY/BANKNIFTY option selling, Zerodha |
| [althk/zerobha](https://github.com/althk/zerobha) | ~50 | Trading bot for NSE with web dashboard |
| [Indian-Algorithmic-Trading-Community](https://github.com/Indian-Algorithmic-Trading-Community) | -- | FOSS community for Indian algo trading |
| [AnjayGoel/algorithmic-trading](https://github.com/AnjayGoel/algorithmic-trading) | ~100 | Ernie Chan strategies adapted for Indian markets |

### India-Specific Data Considerations
- **FII/DII flow data**: Available from NSE website, strong predictive signal
- **India VIX**: Available via APIs, critical for options pricing
- **Expiry day effects**: Unique to Indian markets (weekly expiry Thursdays)
- **Circuit limits**: Upper/lower circuit breakers affect strategy design
- **STT/CTT**: Securities/Commodities Transaction Tax affects profitability of high-frequency strategies
- **Liquidity**: NIFTY/BANKNIFTY options are very liquid; individual stock options much less so

---

## 9. Critical Practical Considerations

### The Reality Check

> **Over 90% of academic trading strategies fail when implemented with real capital, despite generating double-digit annual returns through backtesting.**

### Common Pitfalls

| Pitfall | Description | How to Avoid |
|---------|-------------|--------------|
| **Lookahead bias** | Using future data in feature calculation | Walk-forward validation, rolling windows |
| **Survivorship bias** | Only testing on stocks that still exist | Use full historical universe |
| **Overfitting** | Model memorizes training data | Limit features to 20-30, use regularization |
| **Data snooping** | Testing many strategies until one "works" | Pre-register hypotheses, out-of-sample testing |
| **Ignoring costs** | Not accounting for slippage/commissions | Include realistic costs (0.03-0.05% India) |
| **In-sample testing** | No proper train/test split | Purged K-Fold or Walk-Forward validation |
| **Curve fitting** | Too many parameters for available data | Simple models, fewer parameters |

### Realistic Performance Benchmarks
| Metric | Suspicious | Realistic | Excellent |
|--------|------------|-----------|-----------|
| Annual return | >30% | 10-20% | 15-25% |
| Sharpe ratio | >3.0 | 0.5-1.5 | 1.5-2.5 |
| Max drawdown | <5% | 15-30% | 10-20% |
| Win rate | >70% | 45-55% | 55-60% |
| Profit factor | >3.0 | 1.3-2.0 | 1.5-2.5 |

### Validation Framework (Recommended)
1. **Walk-Forward Analysis**: Train on N months, test on next month, slide forward
2. **Purged K-Fold CV**: K-Fold with gap between train/test to prevent leakage
3. **Multiple market conditions**: Test across bull, bear, and sideways periods
4. **Monte Carlo simulation**: Bootstrap results to estimate confidence intervals
5. **Paper trading**: Minimum 3 months before real capital

### Data Requirements
| Strategy Type | Min Data Needed | Recommended |
|---------------|-----------------|-------------|
| Daily XGBoost | 2 years | 5+ years |
| Intraday LSTM | 6 months (minute) | 1-2 years |
| RL agent | 5+ years | 10+ years |
| HMM regime | 5 years | 10+ years |
| Sentiment | 1 year | 2+ years |

---

## 10. Recommended Path for AutoTheta

Based on this research, here is a prioritized roadmap for integrating ML into AutoTheta, ordered by practical value and feasibility:

### Priority 1: Upgrade Regime Detection (HIGH VALUE, MODERATE EFFORT)
**Current**: Your rule-based BULL/BEAR/CRASH detection using 200-DMA
**Upgrade to**: HMM-based regime detection
- Use `hmmlearn` library with 3-4 states
- Features: daily returns, rolling volatility, volume ratio, India VIX
- Train on 5+ years of NIFTY data
- **Expected benefit**: Better regime transitions, earlier crash detection, reduced drawdowns
- **Risk**: Low (supplements existing system, doesn't replace it)

### Priority 2: XGBoost Signal Confirmation (HIGH VALUE, MODERATE EFFORT)
**Use case**: Confirm/reject trade signals from your existing strategy
- Train XGBoost classifier on your existing features (VWAP dev, ADX, RSI, MFI, KER)
- Target: Whether a signal leads to profitable trade (binary classification)
- Walk-forward validation with 6-month training window
- **Expected benefit**: Filter out 20-30% of losing trades
- **Risk**: Moderate (need sufficient trade history for training)

### Priority 3: Feature Enhancement (MODERATE VALUE, LOW EFFORT)
Add these proven features to your existing system:
- India VIX level and rate of change
- FII/DII flow data (daily)
- US market overnight return (S&P 500 futures)
- Nifty Put-Call Ratio
- Intraday gap percentage
- Volume-weighted returns

### Priority 4: Sentiment Filter (LOW-MODERATE VALUE, HIGH EFFORT)
- Use FinBERT on major financial news feeds
- Binary filter: "Is there strong negative sentiment today?"
- If yes, reduce position size or skip trades
- **Challenge**: Requires news data pipeline, NLP infrastructure

### DO NOT Pursue (for now)
- **LSTM for price prediction**: Low signal-to-noise, overfitting risk, marginal improvement
- **RL trading agents**: Unstable training, massive overfitting, sim-to-real gap
- **Standalone sentiment strategies**: Too weak as primary signal for options trading

---

## Sources

### Research & Articles
- [Advanced Stock Market Prediction Using LSTM Networks (2025)](https://arxiv.org/html/2505.05325v1)
- [Survey of Feature Selection for Stock Market Prediction](https://link.springer.com/article/10.1186/s40854-022-00441-7)
- [The Alpha Scientist - Feature Engineering](https://alphascientist.com/feature_engineering.html)
- [QuantStart - HMM for Market Regime Detection](https://www.quantstart.com/articles/market-regime-detection-using-hidden-markov-models-in-qstrader/)
- [QuantInsti - Regime-Adaptive Trading with HMM + Random Forest](https://blog.quantinsti.com/regime-adaptive-trading-python/)
- [Rigorous Walk-Forward Validation Framework](https://arxiv.org/html/2512.12924v1)
- [Chaos, Overfitting and Equilibrium: Can ML Beat Markets?](https://www.sciencedirect.com/science/article/abs/pii/S105752192400406X)
- [XGBoost Stock Direction with Investor Sentiments](https://www.sciencedirect.com/science/article/abs/pii/S1062940822001838)
- [FinBERT-LSTM for Stock Prediction](https://dl.acm.org/doi/10.1145/3694860.3694870)
- [MarketCalls - XGBoost Stock Prediction](https://www.marketcalls.in/machine-learning/predicting-stock-price-and-market-direction-using-xgboost-machine-learning-algorithm.html)
- [LSEG - Market Regime Detection Approaches](https://developers.lseg.com/en/article-catalog/article/market-regime-detection)
- [Analyzing Alpha - Top 21 Python Trading Tools](https://analyzingalpha.com/python-trading-tools)
- [LSTM Time Series Stock Prediction = FAIL (Kaggle)](https://www.kaggle.com/code/carlmcbrideellis/lstm-time-series-stock-price-prediction-fail)

### GitHub Repositories (by stars)
- [microsoft/qlib](https://github.com/microsoft/qlib) - 39.7k stars - AI quant platform
- [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) - 48k stars - Crypto trading bot
- [mementum/backtrader](https://github.com/mementum/backtrader) - 21k stars - Backtesting framework
- [quantopian/zipline](https://github.com/quantopian/zipline) - 20k stars - Event-driven backtesting
- [QuantConnect/Lean](https://github.com/QuantConnect/Lean) - 18k stars - Algo trading engine
- [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) - 18k stars - Market making
- [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) - 16.9k stars - ML trading book
- [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) - 14.6k stars - RL trading
- [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) - 9.1k stars - HFT platform
- [edtechre/pybroker](https://github.com/edtechre/pybroker) - 3.3k stars - ML backtesting
- [ProsusAI/finBERT](https://github.com/ProsusAI/finBERT) - ~3k stars - Financial sentiment
- [wangzhe3224/awesome-systematic-trading](https://github.com/wangzhe3224/awesome-systematic-trading) - Curated list
- [merovinh/best-of-algorithmic-trading](https://github.com/merovinh/best-of-algorithmic-trading) - Ranked list
- [angel-one/smartapi-python](https://github.com/angel-one/smartapi-python) - Angel One SDK
- [buzzsubash/algo_trading_strategies_india](https://github.com/buzzsubash/algo_trading_strategies_india) - India options
