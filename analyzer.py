# ============================================================
# NCT ANALYZER V2
# MARKET INTELLIGENCE
# NACIONAL_CENTRO_TECNICO
#
# analyzer.py
#
# Função:
#   - Analisa séries M5 de pares Forex
#   - Trabalha somente com candles fechados recebidos
#   - Identifica líderes e seguidores
#   - Considera início do movimento
#   - Considera força e consistência
#   - Considera magnitude do movimento
#   - Compara trajetória normalizada
#   - Calcula similaridade entre os pares
#
# IMPORTANTE:
#   Líder é uma classificação estatística/heurística.
#   Não representa causalidade entre os ativos.
# ============================================================


from math import sqrt, isfinite


# ============================================================
# FUNÇÕES BÁSICAS
# ============================================================

def safe_float(value, default=0.0):
    """
    Converte um valor para float com segurança.
    """
    try:
        result = float(value)

        if not isfinite(result):
            return default

        return result

    except (TypeError, ValueError):
        return default


def clamp(value, minimum=0.0, maximum=100.0):
    """
    Mantém um valor dentro de um intervalo.
    """
    value = safe_float(value)

    if value < minimum:
        return minimum

    if value > maximum:
        return maximum

    return value


def mean_safe(values):
    """
    Média segura.
    """
    if not values:
        return 0.0

    valid = [
        safe_float(v)
        for v in values
        if v is not None
    ]

    if not valid:
        return 0.0

    return sum(valid) / len(valid)


# ============================================================
# DIREÇÃO
# ============================================================

def get_direction(return_pct):
    """
    Retorna a direção textual do movimento.
    """

    return_pct = safe_float(return_pct)

    if return_pct > 0.00001:
        return "ALTA"

    if return_pct < -0.00001:
        return "BAIXA"

    return "NEUTRA"


def get_direction_code(direction):
    """
    Código curto da direção.
    """

    if direction == "ALTA":
        return "up"

    if direction == "BAIXA":
        return "down"

    return "neutral"


def get_direction_icon(direction):
    """
    Ícone da direção.
    """

    if direction == "ALTA":
        return "↑"

    if direction == "BAIXA":
        return "↓"

    return "→"


# ============================================================
# NORMALIZAÇÃO
# ============================================================

def normalized_returns(candles):
    """
    Converte uma série de candles em trajetória percentual
    acumulada a partir do primeiro fechamento.

    Exemplo:

        100
        101
        102
        101

    vira aproximadamente:

        0
        +1
        +2
        +1

    Isso permite comparar ativos com preços absolutos diferentes.
    """

    if not candles:
        return []

    closes = []

    for candle in candles:

        close = safe_float(candle.get("close"))

        if close > 0:
            closes.append(close)

    if len(closes) < 2:
        return []

    base = closes[0]

    if base <= 0:
        return []

    result = []

    for close in closes:

        pct = ((close / base) - 1.0) * 100.0

        result.append(pct)

    return result


# ============================================================
# RETORNO TOTAL
# ============================================================

def calculate_total_return(path):
    """
    Retorno total da trajetória.
    """

    if not path or len(path) < 2:
        return 0.0

    return safe_float(path[-1]) - safe_float(path[0])


# ============================================================
# MAGNITUDE
# ============================================================

def calculate_magnitude(path):
    """
    Mede a magnitude do movimento.

    Não olha apenas o resultado final.

    Considera:
        - maior alta
        - maior baixa
        - amplitude total
    """

    if not path:
        return 0.0

    minimum = min(path)
    maximum = max(path)

    amplitude = maximum - minimum

    return abs(amplitude)


# ============================================================
# FORÇA DO MOVIMENTO
# ============================================================

def calculate_strength(path):
    """
    Mede a força média do deslocamento entre candles.

    Quanto maior o deslocamento médio entre candles,
    maior a força.
    """

    if not path or len(path) < 2:
        return 0.0

    movements = []

    for i in range(1, len(path)):

        previous = safe_float(path[i - 1])
        current = safe_float(path[i])

        movement = abs(current - previous)

        movements.append(movement)

    if not movements:
        return 0.0

    average_move = mean_safe(movements)

    return average_move


# ============================================================
# CONSISTÊNCIA DIRECIONAL
# ============================================================

def calculate_directional_consistency(path):
    """
    Mede quanto do caminho caminhou na direção final.

    Exemplo:

        + + + + +

    Alta consistência.

    Já:

        + - + - +

    menor consistência.
    """

    if not path or len(path) < 2:
        return 0.0

    total_return = calculate_total_return(path)

    if total_return == 0:
        return 0.0

    direction = 1 if total_return > 0 else -1

    favorable = 0.0
    total = 0.0

    for i in range(1, len(path)):

        movement = safe_float(path[i]) - safe_float(path[i - 1])

        total += abs(movement)

        if movement * direction > 0:
            favorable += abs(movement)

    if total == 0:
        return 0.0

    consistency = favorable / total

    return clamp(consistency * 100.0)


# ============================================================
# INÍCIO DO MOVIMENTO
# ============================================================

def calculate_onset(path):
    """
    Identifica em que parte da janela o movimento começou.

    Quanto mais cedo o movimento relevante aparece,
    maior o valor de onset.

    Resultado:
        0 a 100
    """

    if not path or len(path) < 4:
        return 0.0

    total_return = calculate_total_return(path)

    if total_return == 0:
        return 0.0

    direction = 1 if total_return > 0 else -1

    final_abs = abs(total_return)

    if final_abs == 0:
        return 0.0

    threshold = final_abs * 0.20

    onset_index = None

    for i in range(1, len(path)):

        movement_from_start = safe_float(path[i]) - safe_float(path[0])

        directional_move = movement_from_start * direction

        if directional_move >= threshold:

            onset_index = i
            break

    if onset_index is None:
        return 0.0

    total_steps = len(path) - 1

    if total_steps <= 0:
        return 0.0

    # Quanto menor o índice, maior a pontuação.
    position = onset_index / total_steps

    score = (1.0 - position) * 100.0

    return clamp(score)


# ============================================================
# FORÇA INICIAL
# ============================================================

def calculate_early_strength(path):
    """
    Mede quanto do movimento final já apareceu
    nos primeiros candles da janela.

    Isso ajuda a identificar quem começou o movimento
    antes dos demais.
    """

    if not path or len(path) < 4:
        return 0.0

    total_return = calculate_total_return(path)

    if total_return == 0:
        return 0.0

    direction = 1 if total_return > 0 else -1

    final_abs = abs(total_return)

    if final_abs == 0:
        return 0.0

    # Primeiros 30% da janela
    early_count = max(
        2,
        int(len(path) * 0.30)
    )

    early_count = min(
        early_count,
        len(path) - 1
    )

    early_move = (
        safe_float(path[early_count]) -
        safe_float(path[0])
    )

    early_directional = early_move * direction

    if early_directional <= 0:
        return 0.0

    ratio = early_directional / final_abs

    return clamp(ratio * 100.0)


# ============================================================
# CORRELAÇÃO DE PEARSON
# ============================================================

def pearson_correlation(series_a, series_b):
    """
    Correlação de Pearson entre duas trajetórias.
    """

    n = min(
        len(series_a),
        len(series_b)
    )

    if n < 2:
        return 0.0

    a = [
        safe_float(v)
        for v in series_a[:n]
    ]

    b = [
        safe_float(v)
        for v in series_b[:n]
    ]

    mean_a = mean_safe(a)
    mean_b = mean_safe(b)

    numerator = 0.0
    sum_a = 0.0
    sum_b = 0.0

    for i in range(n):

        da = a[i] - mean_a
        db = b[i] - mean_b

        numerator += da * db
        sum_a += da * da
        sum_b += db * db

    denominator = sqrt(
        sum_a * sum_b
    )

    if denominator == 0:
        return 0.0

    correlation = numerator / denominator

    return max(
        -1.0,
        min(1.0, correlation)
    )


# ============================================================
# ACORDO DIRECIONAL
# ============================================================

def directional_agreement(path_a, path_b):
    """
    Mede quantos candles caminharam na mesma direção.
    """

    n = min(
        len(path_a),
        len(path_b)
    )

    if n < 2:
        return 0.0

    same = 0
    total = 0

    for i in range(1, n):

        move_a = (
            safe_float(path_a[i]) -
            safe_float(path_a[i - 1])
        )

        move_b = (
            safe_float(path_b[i]) -
            safe_float(path_b[i - 1])
        )

        if move_a == 0 or move_b == 0:
            continue

        total += 1

        if (
            (move_a > 0 and move_b > 0)
            or
            (move_a < 0 and move_b < 0)
        ):
            same += 1

    if total == 0:
        return 0.0

    return same / total


# ============================================================
# SIMILARIDADE
# ============================================================

def calculate_similarity(path_a, path_b):
    """
    Similaridade entre dois ativos.

    Composição:

        60% correlação da trajetória
        40% acordo direcional

    Retorno:
        0 a 100
    """

    if not path_a or not path_b:
        return 0.0

    correlation = pearson_correlation(
        path_a,
        path_b
    )

    agreement = directional_agreement(
        path_a,
        path_b
    )

    # Correlação -1 até +1
    # Transformamos em 0 até 100.
    correlation_score = (
        (correlation + 1.0) / 2.0
    ) * 100.0

    agreement_score = agreement * 100.0

    similarity = (
        correlation_score * 0.60
        +
        agreement_score * 0.40
    )

    return clamp(similarity)


# ============================================================
# SCORE DO LÍDER
# ============================================================

def calculate_leader_score(
    magnitude,
    strength,
    consistency,
    onset,
    early_strength
):
    """
    Score utilizado para selecionar os líderes.

    Pesos:

        20% magnitude
        20% força
        20% consistência
        25% início
        15% força inicial
    """

    # --------------------------------------------------------
    # Normalizações
    # --------------------------------------------------------

    magnitude_score = clamp(
        magnitude * 10.0
    )

    strength_score = clamp(
        strength * 100.0
    )

    consistency_score = clamp(
        consistency
    )

    onset_score = clamp(
        onset
    )

    early_score = clamp(
        early_strength
    )

    score = (
        magnitude_score * 0.20
        +
        strength_score * 0.20
        +
        consistency_score * 0.20
        +
        onset_score * 0.25
        +
        early_score * 0.15
    )

    return clamp(score)


# ============================================================
# PREPARAÇÃO DE CADA PAR
# ============================================================

def prepare_pair(symbol, candles):
    """
    Prepara todas as métricas de um par.
    """

    if not candles:
        return None

    path = normalized_returns(candles)

    if len(path) < 2:
        return None

    total_return = calculate_total_return(path)

    magnitude = calculate_magnitude(path)

    strength = calculate_strength(path)

    consistency = (
        calculate_directional_consistency(path)
    )

    onset = calculate_onset(path)

    early_strength = calculate_early_strength(path)

    direction = get_direction(
        total_return
    )

    direction_code = get_direction_code(
        direction
    )

    direction_icon = get_direction_icon(
        direction
    )

    return {
        "symbol": symbol,

        "path": path,

        "return_pct": round(
            total_return,
            4
        ),

        "magnitude": round(
            magnitude,
            4
        ),

        "strength": round(
            strength,
            4
        ),

        "consistency": round(
            consistency,
            2
        ),

        "onset": round(
            onset,
            2
        ),

        "early_strength": round(
            early_strength,
            2
        ),

        "direction": direction,

        "direction_code": direction_code,

        "direction_icon": direction_icon
    }


# ============================================================
# ANÁLISE PRINCIPAL
# ============================================================

def analyze_market(
    market_data,
    window=12,
    leaders=4,
    followers_per_leader=6,
    min_similarity=55,

    # Compatibilidade com versões anteriores
    number_of_leaders=None,
    minimum_similarity=None,

    **kwargs
):
    """
    Executa a análise completa.

    Compatibilidade:

        leaders
        followers_per_leader
        min_similarity

    Também aceita:

        number_of_leaders
        minimum_similarity

    Isso evita quebra entre versões do app.py.
    """

    # ========================================================
    # COMPATIBILIDADE DE PARÂMETROS
    # ========================================================

    if number_of_leaders is not None:
        leaders = number_of_leaders

    if minimum_similarity is not None:
        min_similarity = minimum_similarity

    # ========================================================
    # NORMALIZAÇÃO DOS PARÂMETROS
    # ========================================================

    try:
        window = int(window)
    except:
        window = 12

    try:
        leaders = int(leaders)
    except:
        leaders = 4

    try:
        followers_per_leader = int(
            followers_per_leader
        )
    except:
        followers_per_leader = 6

    try:
        min_similarity = float(
            min_similarity
        )
    except:
        min_similarity = 55.0

    window = max(
        2,
        window
    )

    leaders = max(
        1,
        leaders
    )

    followers_per_leader = max(
        1,
        followers_per_leader
    )

    min_similarity = clamp(
        min_similarity,
        0,
        100
    )

    # ========================================================
    # VALIDAÇÃO DOS DADOS
    # ========================================================

    if not market_data:
        return {
            "leaders": [],
            "symbols_analyzed": 0,
            "error": "Nenhum dado de mercado disponível."
        }

    # ========================================================
    # PREPARAÇÃO DOS PARES
    # ========================================================

    prepared = []

    for symbol, candles in market_data.items():

        if not candles:
            continue

        # ----------------------------------------------------
        # Mantém somente a janela solicitada
        # ----------------------------------------------------

        candles = list(candles)

        if len(candles) > window:
            candles = candles[-window:]

        # ----------------------------------------------------
        # Exige dados suficientes
        # ----------------------------------------------------

        if len(candles) < 2:
            continue

        pair = prepare_pair(
            symbol,
            candles
        )

        if pair is None:
            continue

        prepared.append(
            pair
        )

    # ========================================================
    # QUANTIDADE DE ATIVOS
    # ========================================================

    symbols_analyzed = len(
        prepared
    )

    if symbols_analyzed == 0:
        return {
            "leaders": [],
            "symbols_analyzed": 0,
            "error": "Nenhum par possui candles suficientes para análise."
        }

    # ========================================================
    # NORMALIZAÇÃO DO SCORE
    # ========================================================
    #
    # Para evitar que a magnitude absoluta domine o resultado,
    # fazemos uma normalização relativa entre os pares.
    #
    # ========================================================

    magnitudes = [
        safe_float(
            item["magnitude"]
        )
        for item in prepared
    ]

    strengths = [
        safe_float(
            item["strength"]
        )
        for item in prepared
    ]

    max_magnitude = max(
        magnitudes
    ) if magnitudes else 1.0

    max_strength = max(
        strengths
    ) if strengths else 1.0

    if max_magnitude <= 0:
        max_magnitude = 1.0

    if max_strength <= 0:
        max_strength = 1.0

    # ========================================================
    # SCORE DOS CANDIDATOS
    # ========================================================

    for item in prepared:

        magnitude_relative = (
            item["magnitude"]
            /
            max_magnitude
        ) * 10.0

        strength_relative = (
            item["strength"]
            /
            max_strength
        ) * 1.0

        score = calculate_leader_score(
            magnitude=magnitude_relative,
            strength=strength_relative,
            consistency=item["consistency"],
            onset=item["onset"],
            early_strength=item["early_strength"]
        )

        item["score"] = round(
            score,
            2
        )

    # ========================================================
    # ORDENAÇÃO DOS CANDIDATOS
    # ========================================================
    #
    # Primeiro:
    #   score geral
    #
    # Desempate:
    #   onset
    #   early_strength
    #   consistência
    #
    # ========================================================

    prepared.sort(
        key=lambda item: (
            item["score"],
            item["onset"],
            item["early_strength"],
            item["consistency"]
        ),
        reverse=True
    )

    # ========================================================
    # SELEÇÃO DOS LÍDERES
    # ========================================================

    selected_leaders = []

    for candidate in prepared:

        if len(selected_leaders) >= leaders:
            break

        # ----------------------------------------------------
        # Evita colocar pares extremamente semelhantes
        # como líderes independentes.
        #
        # Se já existe um líder muito semelhante,
        # ele é melhor tratado como seguidor.
        # ----------------------------------------------------

        too_similar_to_existing = False

        for existing in selected_leaders:

            similarity = calculate_similarity(
                candidate["path"],
                existing["path"]
            )

            # Se são praticamente o mesmo movimento,
            # evitamos duplicar liderança.
            if similarity >= 92:
                too_similar_to_existing = True
                break

        if too_similar_to_existing:
            continue

        selected_leaders.append(
            candidate
        )

    # ========================================================
    # CASO NÃO TENHA CONSEGUIDO O NÚMERO SOLICITADO
    # ========================================================

    if len(selected_leaders) < leaders:

        for candidate in prepared:

            if candidate in selected_leaders:
                continue

            selected_leaders.append(
                candidate
            )

            if len(selected_leaders) >= leaders:
                break

    # ========================================================
    # CONSTRUÇÃO DOS CARDS DOS LÍDERES
    # ========================================================

    result_leaders = []

    for leader in selected_leaders:

        followers = []

        for candidate in prepared:

            if candidate["symbol"] == leader["symbol"]:
                continue

            similarity = calculate_similarity(
                leader["path"],
                candidate["path"]
            )

            # ------------------------------------------------
            # Só entra como seguidor se atingir o mínimo
            # configurado.
            # ------------------------------------------------

            if similarity < min_similarity:
                continue

            follower = {
                "symbol": candidate["symbol"],

                "similarity": round(
                    similarity,
                    2
                ),

                "direction": candidate["direction"],

                "direction_code": candidate[
                    "direction_code"
                ],

                "direction_icon": candidate[
                    "direction_icon"
                ],

                "return_pct": round(
                    candidate["return_pct"],
                    4
                )
            }

            followers.append(
                follower
            )

        # ----------------------------------------------------
        # Ordena seguidores pela maior similaridade
        # ----------------------------------------------------

        followers.sort(
            key=lambda item: (
                item["similarity"],
                abs(
                    safe_float(
                        item["return_pct"]
                    )
                )
            ),
            reverse=True
        )

        followers = followers[
            :followers_per_leader
        ]

        # ----------------------------------------------------
        # Card final
        # ----------------------------------------------------

        leader_card = {
            "symbol": leader["symbol"],

            "direction": leader[
                "direction"
            ],

            "direction_code": leader[
                "direction_code"
            ],

            "direction_icon": leader[
                "direction_icon"
            ],

            "return_pct": round(
                leader["return_pct"],
                4
            ),

            "score": round(
                leader["score"],
                2
            ),

            "strength": round(
                leader["strength"],
                4
            ),

            "consistency": round(
                leader["consistency"],
                2
            ),

            "onset": round(
                leader["onset"],
                2
            ),

            "early_strength": round(
                leader["early_strength"],
                2
            ),

            "followers": followers
        }

        result_leaders.append(
            leader_card
        )

    # ========================================================
    # RESULTADO FINAL
    # ========================================================

    return {
        "leaders": result_leaders,

        "symbols_analyzed": symbols_analyzed,

        "window": window,

        "minimum_similarity": min_similarity,

        "followers_per_leader": followers_per_leader
    }


# ============================================================
# TESTE LOCAL
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("NCT ANALYZER V2")
    print("TESTE LOCAL DO ANALYZER.PY")
    print("=" * 60)

    # --------------------------------------------------------
    # Dados fictícios apenas para verificar funcionamento.
    # Não representam mercado real.
    # --------------------------------------------------------

    test_data = {

        "EUR/USD": [
            {"close": 1.1000},
            {"close": 1.1005},
            {"close": 1.1010},
            {"close": 1.1018},
            {"close": 1.1022},
            {"close": 1.1028},
            {"close": 1.1034},
            {"close": 1.1040},
            {"close": 1.1044},
            {"close": 1.1050},
            {"close": 1.1055},
            {"close": 1.1060},
        ],

        "GBP/USD": [
            {"close": 1.2500},
            {"close": 1.2502},
            {"close": 1.2508},
            {"close": 1.2510},
            {"close": 1.2518},
            {"close": 1.2524},
            {"close": 1.2530},
            {"close": 1.2534},
            {"close": 1.2540},
            {"close": 1.2543},
            {"close": 1.2548},
            {"close": 1.2552},
        ],

        "USD/JPY": [
            {"close": 150.00},
            {"close": 149.98},
            {"close": 149.95},
            {"close": 149.92},
            {"close": 149.90},
            {"close": 149.88},
            {"close": 149.84},
            {"close": 149.82},
            {"close": 149.80},
            {"close": 149.77},
            {"close": 149.75},
            {"close": 149.72},
        ],

        "USD/CHF": [
            {"close": 0.9000},
            {"close": 0.9001},
            {"close": 0.9002},
            {"close": 0.9004},
            {"close": 0.9003},
            {"close": 0.9005},
            {"close": 0.9007},
            {"close": 0.9008},
            {"close": 0.9010},
            {"close": 0.9012},
            {"close": 0.9013},
            {"close": 0.9015},
        ],

        "AUD/USD": [
            {"close": 0.6600},
            {"close": 0.6601},
            {"close": 0.6600},
            {"close": 0.6602},
            {"close": 0.6601},
            {"close": 0.6603},
            {"close": 0.6602},
            {"close": 0.6604},
            {"close": 0.6603},
            {"close": 0.6605},
            {"close": 0.6604},
            {"close": 0.6606},
        ]
    }

    result = analyze_market(
        market_data=test_data,
        window=12,
        number_of_leaders=4,
        followers_per_leader=6,
        minimum_similarity=55
    )

    print()

    print(
        "PARES ANALISADOS:",
        result.get("symbols_analyzed")
    )

    print()

    for index, leader in enumerate(
        result.get("leaders", []),
        start=1
    ):

        print(
            f"{index}. "
            f"{leader['symbol']} "
            f"{leader['direction']} "
            f"score={leader['score']}"
        )

        print(
            f"   Retorno: "
            f"{leader['return_pct']}%"
        )

        print(
            f"   Início: "
            f"{leader['onset']}"
        )

        print(
            f"   Força inicial: "
            f"{leader['early_strength']}"
        )

        print(
            f"   Seguidores: "
            f"{len(leader['followers'])}"
        )

        for follower in leader[
            "followers"
        ]:

            print(
                f"      "
                f"{follower['symbol']} "
                f"{follower['similarity']}%"
            )

        print()

    print("=" * 60)
    print("TESTE FINALIZADO")
    print("=" * 60)