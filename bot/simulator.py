"""
Price Simulator — Paper Trading Mode
Simulates price movement for testing without real money.
"""

import threading
import time
import random
import math
from typing import Callable, Optional


class PriceSimulator:
    """
    Simulates realistic forex price movement using Geometric Brownian Motion.
    Perfect for paper trading and backtesting the martingale strategy.
    """

    def __init__(self, config: dict, on_tick: Optional[Callable] = None):
        self.cfg = config
        self.on_tick = on_tick

        self.symbol = config["symbol"]
        self.spread = config["simulator"]["spread_points"]

        # Starting price (XAUUSD range)
        self.price = 2650.0
        self.running = False
        self._thread: Optional[threading.Thread] = None

        # GBM parameters
        self.mu = 0.0          # drift (mean return)
        self.sigma = 0.002     # volatility
        self.dt = 0.1          # time step (seconds)

    def set_starting_price(self, price: float):
        self.price = price

    def start(self, interval_ms: int = 300):
        self.running = True
        self._thread = threading.Thread(
            target=self._price_loop,
            args=(interval_ms,),
            daemon=True
        )
        self._thread.start()
        print(f"[SIM] Price simulator started at {self.price}")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)
        print("[SIM] Simulator stopped.")

    def _price_loop(self, interval_ms: int):
        while self.running:
            # Geometric Brownian Motion step
            epsilon = random.gauss(0, 1)
            self.price *= math.exp(
                (self.mu - 0.5 * self.sigma ** 2) * self.dt
                + self.sigma * math.sqrt(self.dt) * epsilon
            )
            self.price = round(self.price, 2)

            if self.on_tick:
                try:
                    self.on_tick(self.price)
                except Exception as e:
                    print(f"[SIM] Tick callback error: {e}")

            time.sleep(interval_ms / 1000)

    def get_current_price(self) -> float:
        return round(self.price, 2)

    def set_scenario(self, scenario: str):
        """
        Predefined price scenarios for testing.
        - 'trending_down': aggressive downtrend (tests martingale depth)
        - 'ranging': sideways (best case for martingale)
        - 'trending_up': uptrend (quick wins for BUY)
        """
        scenarios = {
            "trending_down": {"mu": -0.0005, "sigma": 0.001},
            "ranging":       {"mu": 0.0,     "sigma": 0.0015},
            "trending_up":   {"mu": 0.0005,  "sigma": 0.001},
            "volatile":      {"mu": 0.0,     "sigma": 0.004},
        }
        if scenario in scenarios:
            self.mu = scenarios[scenario]["mu"]
            self.sigma = scenarios[scenario]["sigma"]
            print(f"[SIM] Scenario set to: {scenario}")
