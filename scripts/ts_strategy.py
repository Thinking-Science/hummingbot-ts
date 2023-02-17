import datetime
import os
import pickle
from decimal import Decimal
from typing import Dict, List

import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import BaseModel

from hummingbot import data_path
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.smart_components.position_executor.data_types import PositionConfig
from hummingbot.smart_components.position_executor.position_executor import PositionExecutor
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from models.utils.add_features import Features


class BotProfile(BaseModel):
    order_amount_usd: Decimal
    long_threshold: float
    short_threshold: float
    leverage: int
    max_executors: int
    stop_loss: float
    take_profit: float
    time_limit: int


class SignalExecutor(PositionExecutor):
    def __init__(self, position_config: PositionConfig, strategy: ScriptStrategyBase, signal_value: int):
        super().__init__(position_config, strategy)
        self.signal_value = signal_value


class AdvancedDirectionalStrategyExample(ScriptStrategyBase):
    """
    A directional trading strategy that uses two timeframes of the same trading pair (ETH-USDT)
    and based on the RSI and Bollinger Bands is going long or short.
    The weigths of the indicators are the same, that means that 0.5 * RSI + 0.5 * BBANDS = signal_value, but we are
    going to prioritize 1m timeframe so after calculating the signal for 1m and 3m, we are going to multiply the value
    of 1m by 0.7 and 3m by 0.3.
    IMPORTANT: Binance perpetual has to be in Single Asset Mode, soon we are going to support Multi Asset Mode.
    """
    bot_profile = BotProfile(
        order_amount_usd=Decimal(100),
        long_threshold=0.5,
        short_threshold=-0.5,
        leverage=5,
        max_executors=1,
        stop_loss=0.002,
        take_profit=0.004,
        time_limit=60 * 2,
    )
    trading_pair = "ETH-BUSD"
    exchange = "binance_perpetual"

    eth_1m_candles = CandlesFactory.get_candle(connector=exchange,
                                               trading_pair=trading_pair,
                                               interval="1m", max_records=50)

    candles = {
        f"{trading_pair}_1m": eth_1m_candles,
    }

    ft = Features()
    model_file = pickle.load(open('models/metajohnny.pkl', 'rb'))
    model_metadata = model_file['metadata']
    model = model_file['object']

    set_leverage_flag = None
    active_signal_executors: List[SignalExecutor] = []
    stored_signal_executors: List[SignalExecutor] = []

    today = datetime.datetime.today()
    csv_path = data_path() + f"/{exchange}_{trading_pair}_{today.day:02d}-{today.month:02d}-{today.year}.csv"
    markets = {exchange: {trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        # Is necessary to start the Candles Feed.
        super().__init__(connectors)
        for candle in self.candles.values():
            candle.start()

    def get_active_executors(self):
        return [signal_executor for signal_executor in self.active_signal_executors
                if not signal_executor.is_closed]

    def get_closed_executors(self):
        return self.stored_signal_executors

    @property
    def all_candles_ready(self):
        """
        Checks if the candlesticks are full.
        """
        return all([candle.is_ready for candle in self.candles.values()])

    def on_tick(self):
        self.check_and_set_leverage()
        if len(self.get_active_executors()) < self.bot_profile.max_executors and self.is_margin_enough():
            signal_value = self.get_signal()
            if signal_value > self.bot_profile.long_threshold or signal_value < self.bot_profile.short_threshold:
                price = self.connectors[self.exchange].get_mid_price(self.trading_pair)
                signal_executor = SignalExecutor(
                    position_config=PositionConfig(
                        timestamp=self.current_timestamp, trading_pair=self.trading_pair,
                        exchange=self.exchange, order_type=OrderType.MARKET,
                        side=PositionSide.LONG if signal_value > 0 else PositionSide.SHORT,
                        entry_price=price,
                        amount=self.bot_profile.order_amount_usd / price,
                        stop_loss=self.bot_profile.stop_loss,
                        take_profit=self.bot_profile.take_profit,
                        time_limit=self.bot_profile.time_limit),
                    strategy=self,
                    signal_value=signal_value
                )
                self.active_signal_executors.append(signal_executor)
        self.clean_and_store_executors()

    def get_signal(self):
        for candle_name, candle in self.candles.items():
            candle_df = self.ft.add_features(candle.candles_df, self.model_metadata['ADD_FEATURES_DICT'], dropna=True)
            predict = self.model.predict(candle_df)[-1]
            predict_proba = self.model.predict_proba(candle_df).max()
            if predict == 1:
                value = 0
            elif predict == 2:
                value = predict_proba
            else:
                value = -predict_proba
        return value

    def on_stop(self):
        """
        Without this functionality, the network iterator will continue running forever after stopping the strategy
        That's why is necessary to introduce this new feature to make a custom stop with the strategy.
        """
        self.close_open_positions()
        for candle in self.candles.values():
            candle.stop()

    def format_status(self) -> str:
        """
        Displays the three candlesticks involved in the script with RSI, BBANDS and EMA.
        """
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []

        if len(self.stored_signal_executors) > 0:
            lines.extend([
                "\n########################################## Closed Executors ##########################################"])

        for executor in self.stored_signal_executors:
            lines.extend([f"|Signal id: {executor.timestamp} | Signal value: {executor.signal_value}"])
            lines.extend(executor.to_format_status())
            lines.extend([
                "-----------------------------------------------------------------------------------------------------------"])

        if len(self.active_signal_executors) > 0:
            lines.extend([
                "\n########################################## Active Executors ##########################################"])

        for executor in self.active_signal_executors:
            lines.extend([f"|Signal id: {executor.timestamp} | Signal value: {executor.signal_value}"])
            lines.extend(executor.to_format_status())
        if self.all_candles_ready:
            lines.extend([
                "\n############################################ Market Data ############################################\n"])
            lines.extend([f"Value: {self.get_signal()}"])
            columns_to_show = ["timestamp", "open", "low", "high", "close", "volume", "RSI_14", "RSI_14_SIDE", "MACD_12_26_9", "MACD_12_26_9_SIDE"]
            for candle_name, candles in self.candles.items():
                candles_df = self.ft.add_features(candles.candles_df, self.model_metadata['ADD_FEATURES_DICT'], dropna=True)
                candles_df.loc[:, "timestamp"] = pd.to_datetime(candles_df["timestamp"], unit="ms")
                lines.extend([f"Candles: {candles.name} | Interval: {candles.interval}\n"])
                lines.extend(["    " + line for line in candles_df[columns_to_show].tail().to_string(index=False).split("\n")])

            lines.extend(["\n-----------------------------------------------------------------------------------------------------------\n"])
        else:
            lines.extend(["", "  No data collected."])

        return "\n".join(lines)

    def check_and_set_leverage(self):
        if not self.set_leverage_flag:
            for connector in self.connectors.values():
                for trading_pair in connector.trading_pairs:
                    connector.set_position_mode(PositionMode.HEDGE)
                    connector.set_leverage(trading_pair=trading_pair, leverage=self.bot_profile.leverage)
            self.set_leverage_flag = True

    def clean_and_store_executors(self):
        executors_to_store = [executor for executor in self.active_signal_executors if executor.is_closed]
        if not os.path.exists(self.csv_path):
            df_header = pd.DataFrame([("timestamp",
                                       "signal_value",
                                       "exchange",
                                       "trading_pair",
                                       "side",
                                       "amount",
                                       "pnl",
                                       "close_timestamp",
                                       "entry_price",
                                       "close_price",
                                       "last_status",
                                       "sl",
                                       "tp",
                                       "tl",
                                       "order_type",
                                       "leverage")])
            df_header.to_csv(self.csv_path, mode='a', header=False, index=False)
        for executor in executors_to_store:
            self.stored_signal_executors.append(executor)
            df = pd.DataFrame([(executor.timestamp,
                                executor.signal_value,
                                executor.exchange,
                                executor.trading_pair,
                                executor.side,
                                executor.amount,
                                executor.pnl,
                                executor.close_timestamp,
                                executor.entry_price,
                                executor.close_price,
                                executor.status,
                                executor.position_config.stop_loss,
                                executor.position_config.take_profit,
                                executor.position_config.time_limit,
                                executor.open_order_type,
                                self.bot_profile.leverage,)])
            df.to_csv(self.csv_path, mode='a', header=False, index=False)
        self.active_signal_executors = [executor for executor in self.active_signal_executors if not executor.is_closed]

    def close_open_positions(self):
        # we are going to close all the open positions when the bot stops
        for connector_name, connector in self.connectors.items():
            for trading_pair, position in connector.account_positions.items():
                if position.position_side == PositionSide.LONG:
                    self.sell(connector_name=connector_name,
                              trading_pair=position.trading_pair,
                              amount=abs(position.amount),
                              order_type=OrderType.MARKET,
                              price=connector.get_mid_price(position.trading_pair),
                              position_action=PositionAction.CLOSE)
                elif position.position_side == PositionSide.SHORT:
                    self.buy(connector_name=connector_name,
                             trading_pair=position.trading_pair,
                             amount=abs(position.amount),
                             order_type=OrderType.MARKET,
                             price=connector.get_mid_price(position.trading_pair),
                             position_action=PositionAction.CLOSE)

    def is_margin_enough(self):
        quote_balance = self.connectors[self.exchange].get_available_balance(self.trading_pair.split("-")[-1])
        if self.bot_profile.order_amount_usd < quote_balance * self.bot_profile.leverage:
            return True
        else:
            self.logger().info("No enough margin to place orders.")
            return False
