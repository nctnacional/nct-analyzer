
import os
import logging
from typing import Optional, List, Dict, Any

import requests


logger = logging.getLogger(__name__)


class TwelveDataSource:
    """
    Fonte de dados Twelve Data para o NCT ANALYZER V2 ONLINE.

    No modo online, passe as chaves do próprio usuário:
        TwelveDataSource(api_keys=[chave1, chave2, chave3, chave4])

    As chaves não são compartilhadas entre instâncias.
    """

    BASE_URL = "https://api.twelvedata.com"

    def __init__(
        self,
        api_keys: Optional[List[str]] = None,
        timeout: int = 20
    ):
        # As chaves recebidas do usuário têm prioridade.
        # A leitura do ambiente é mantida para compatibilidade local.
        if api_keys is None:
            api_keys = [
                os.getenv(f"TWELVE_DATA_API_KEY_{i}", "").strip()
                for i in range(1, 5)
            ]

            # Compatibilidade com a configuração local antiga.
            if not any(api_keys):
                old_key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
                if old_key:
                    api_keys = [old_key]

        # Remove espaços, entradas vazias e duplicatas.
        self.api_keys = list(
            dict.fromkeys(
                key.strip()
                for key in (api_keys or [])
                if isinstance(key, str) and key.strip()
            )
        )

        self.timeout = timeout
        self.session = requests.Session()
        self.last_error = None
        self.last_status = None

    def set_api_keys(self, api_keys: List[str]) -> None:
        """Atualiza as chaves desta instância."""
        self.api_keys = list(
            dict.fromkeys(
                key.strip()
                for key in (api_keys or [])
                if isinstance(key, str) and key.strip()
            )
        )
        self.last_error = None
        self.last_status = None

    @staticmethod
    def mask_key(key: str) -> str:
        """Retorna uma versão mascarada da chave."""
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + ("*" * (len(key) - 8)) + key[-4:]

    def _extract_error(self, payload: Any) -> str:
        """Extrai uma mensagem de erro sem expor a chave."""
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("code")
            if message:
                return str(message)
        return "A API não retornou dados válidos."

    @staticmethod
    def _is_key_or_limit_error(status_code: int, message: str) -> bool:
        """Identifica erros nos quais faz sentido tentar outra chave."""
        text = (message or "").lower()

        if status_code in (401, 403, 429):
            return True

        indicators = (
            "invalid api key",
            "api key is invalid",
            "unauthorized",
            "rate limit",
            "quota",
            "credits",
            "api credits",
            "too many requests",
            "limit exceeded",
        )

        return any(term in text for term in indicators)

    def _request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        api_keys: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Executa uma requisição.

        Pode tentar outra chave configurada após erro de autenticação
        ou limite. Não registra URLs, parâmetros ou chaves.
        """
        keys = api_keys if api_keys is not None else self.api_keys
        keys = [key.strip() for key in keys if key and key.strip()]

        if not keys:
            self.last_error = "Nenhuma chave da Twelve Data foi configurada."
            self.last_status = None
            return None

        request_params = dict(params or {})
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"

        last_error = None
        last_status = None

        for index, key in enumerate(keys):
            current_params = dict(request_params)
            current_params["apikey"] = key

            try:
                response = self.session.get(
                    url,
                    params=current_params,
                    timeout=self.timeout,
                )

                last_status = response.status_code

                try:
                    payload = response.json()
                except ValueError:
                    payload = {}

                if response.ok and isinstance(payload, dict):
                    if payload.get("status") == "error" or payload.get("code"):
                        message = self._extract_error(payload)
                        last_error = message

                        if (
                            index < len(keys) - 1
                            and self._is_key_or_limit_error(
                                response.status_code, message
                            )
                        ):
                            continue

                        break

                    self.last_error = None
                    self.last_status = response.status_code
                    return payload

                message = self._extract_error(payload)
                last_error = message

                if (
                    index < len(keys) - 1
                    and self._is_key_or_limit_error(
                        response.status_code, message
                    )
                ):
                    continue

                break

            except requests.Timeout:
                last_error = "Tempo limite excedido ao consultar a Twelve Data."
                last_status = None
                # Falha de rede: tentar a próxima chave não garante
                # que o problema seja da chave, mas permite nova tentativa.
                if index < len(keys) - 1:
                    continue
                break

            except requests.RequestException:
                last_error = "Falha de conexão com a Twelve Data."
                last_status = None
                if index < len(keys) - 1:
                    continue
                break

        self.last_error = last_error or "Não foi possível consultar a API."
        self.last_status = last_status

        # Não registrar a URL ou os parâmetros: podem conter a chave.
        logger.warning(
            "Consulta Twelve Data falhou. endpoint=%s status=%s",
            endpoint,
            last_status,
        )

        return None

    def get_candles(
        self,
        symbol: str,
        interval: str = "5min",
        outputsize: int = 12,
    ) -> List[Dict[str, Any]]:
        """Obtém candles em ordem cronológica, do mais antigo ao recente."""
        symbol = (symbol or "").strip().upper()

        if not symbol:
            self.last_error = "Símbolo não informado."
            return []

        allowed_intervals = {
            "1min", "5min", "15min", "30min",
            "45min", "1h", "2h", "4h", "1day",
            "1week", "1month",
        }

        if interval not in allowed_intervals:
            self.last_error = "Intervalo de candles inválido."
            return []

        outputsize = max(1, min(int(outputsize), 5000))

        payload = self._request(
            "time_series",
            {
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
                "format": "JSON",
            },
        )

        if not payload:
            return []

        values = payload.get("values", [])

        if not isinstance(values, list):
            self.last_error = "Formato de candles inesperado."
            return []

        candles = []

        for item in values:
            if not isinstance(item, dict):
                continue

            try:
                candle = {
                    "datetime": item.get("datetime"),
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"]),
                }

                if item.get("volume") not in (None, ""):
                    candle["volume"] = float(item["volume"])

                candles.append(candle)

            except (KeyError, TypeError, ValueError):
                continue

        # A API normalmente entrega os candles do mais recente
        # para o mais antigo; o analisador precisa da ordem cronológica.
        candles.reverse()
        return candles

    def get_candles_batch(
        self,
        symbols: List[str],
        interval: str = "5min",
        outputsize: int = 12,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Consulta vários símbolos sequencialmente."""
        results = {}

        for symbol in symbols:
            normalized_symbol = (symbol or "").strip().upper()
            if not normalized_symbol:
                continue

            results[normalized_symbol] = self.get_candles(
                normalized_symbol,
                interval=interval,
                outputsize=outputsize,
            )

        return results

    def get_forex_pairs(self) -> List[str]:
        """Retorna os pares forex disponíveis na API."""
        payload = self._request("forex_pairs")

        if not payload:
            return []

        items = payload.get("data", [])
        pairs = []

        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    symbol = item.get("symbol")
                    if symbol:
                        pairs.append(str(symbol).strip().upper())

        return sorted(set(pairs))

    def test_connection(
        self,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Testa uma chave específica ou a configuração desta instância."""
        keys = [api_key.strip()] if isinstance(api_key, str) and api_key.strip() else self.api_keys

        if not keys:
            return {
                "success": False,
                "message": "Informe uma chave da Twelve Data.",
            }

        # Usa uma consulta pequena para verificar autenticação e acesso.
        payload = self._request(
            "time_series",
            {
                "symbol": "EUR/USD",
                "interval": "1min",
                "outputsize": 1,
                "format": "JSON",
            },
            api_keys=keys,
        )

        if payload and isinstance(payload.get("values"), list):
            return {
                "success": True,
                "message": "Conexão com a Twelve Data confirmada.",
            }

        return {
            "success": False,
            "message": self.last_error or "Não foi possível validar a chave.",
        }

    def test_all_keys(self) -> List[Dict[str, Any]]:
        """Testa cada chave individualmente, sem misturar os resultados."""
        results = []

        for index, key in enumerate(self.api_keys, start=1):
            result = self.test_connection(api_key=key)

            results.append({
                "key_number": index,
                "masked_key": self.mask_key(key),
                "success": result["success"],
                "message": result["message"],
            })

        return results
