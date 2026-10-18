"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const $ = (id) => document.getElementById(id);

    let requestInProgress = false;
    let refreshTimer = null;
    let analysisEnabled = false;
    let marketChart = null;

    // =====================================================
    // UTILITÁRIOS
    // =====================================================

    function setText(id, value) {
        const element = $(id);
        if (!element) return;

        element.textContent =
            value === null || value === undefined || value === ""
                ? "—"
                : String(value);
    }

    function formatDate(value) {
        if (!value) return "Ainda não realizada";

        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);

        return date.toLocaleString("pt-BR");
    }

    function formatNumber(value, digits = 2) {
        const number = Number(value);

        if (!Number.isFinite(number)) return "—";

        return number.toLocaleString("pt-BR", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        });
    }

    function formatPercent(value) {
        const number = Number(value);

        if (!Number.isFinite(number)) return "—";

        const percent =
            number > 0 && number <= 1 ? number * 100 : number;

        return `${formatNumber(percent)}%`;
    }

    function showMessage(id, message, type = "info") {
        let element = $(id);

        if (!element) {
            const anchor =
                id === "config-message"
                    ? $("config-form")
                    : id === "key-results"
                        ? $("test-all-keys")
                        : $("start-analysis");

            if (!anchor) return;

            element = document.createElement("div");
            element.id = id;
            element.className = "mb-3 rounded-lg border p-3 text-xs";

            anchor.parentNode.insertBefore(
                element,
                anchor.nextSibling
            );
        }

        element.textContent = message || "";

        element.classList.remove(
            "hidden",
            "bg-rose-500/10",
            "border-rose-500/20",
            "text-rose-400",
            "bg-emerald-500/10",
            "border-emerald-500/20",
            "text-emerald-400",
            "bg-amber-500/10",
            "border-amber-500/20",
            "text-amber-400",
            "bg-sky-500/10",
            "border-sky-500/20",
            "text-sky-400"
        );

        if (!message) {
            element.classList.add("hidden");
            return;
        }

        const styles = {
            error: [
                "bg-rose-500/10",
                "border-rose-500/20",
                "text-rose-400"
            ],
            success: [
                "bg-emerald-500/10",
                "border-emerald-500/20",
                "text-emerald-400"
            ],
            warning: [
                "bg-amber-500/10",
                "border-amber-500/20",
                "text-amber-400"
            ],
            info: [
                "bg-sky-500/10",
                "border-sky-500/20",
                "text-sky-400"
            ]
        };

        element.classList.add(...(styles[type] || styles.info));
    }

    function setLoading(button, loading, message = "Aguarde...") {
        if (!button) return;

        if (loading) {
            if (!button.dataset.originalText) {
                button.dataset.originalText = button.textContent;
            }

            button.disabled = true;
            button.textContent = message;
        } else {
            button.disabled = false;

            if (button.dataset.originalText) {
                button.textContent = button.dataset.originalText;
                delete button.dataset.originalText;
            }
        }
    }

    function csrfToken() {
        const meta = document.querySelector(
            'meta[name="csrf-token"]'
        );

        if (meta) return meta.content;

        const input = document.querySelector(
            'input[name="csrf_token"]'
        );

        return input ? input.value : "";
    }

    // =====================================================
    // COMUNICAÇÃO COM O FLASK
    // =====================================================

    async function api(url, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        const headers = new Headers(options.headers || {});

        headers.set("Accept", "application/json");

        if (method !== "GET" && method !== "HEAD") {
            const token = csrfToken();

            if (token) {
                headers.set("X-CSRFToken", token);
            }

            if (options.body !== undefined) {
                headers.set("Content-Type", "application/json");
            }
        }

        const response = await fetch(url, {
            ...options,
            method,
            headers,
            credentials: "same-origin",
            cache: "no-store"
        });

        const contentType =
            response.headers.get("content-type") || "";

        let data;

        if (contentType.includes("application/json")) {
            data = await response.json();
        } else {
            const responseText = await response.text();

            data = { message: responseText };
        }

        if (!response.ok || data.success === false) {
            if (response.status === 401) {
                throw new Error(
                    "Sessão expirada. Entre novamente no sistema."
                );
            }

            if (response.status === 403) {
                throw new Error(
                    "Acesso negado. Verifique as permissões da conta."
                );
            }

            throw new Error(
                data.error ||
                data.message ||
                `Erro HTTP ${response.status}`
            );
        }

        return data;
    }

    // =====================================================
    // CONFIGURAÇÕES
    // =====================================================

    function getConfigFromForm() {
        return {
            timeframe: $("timeframe")?.value || "5min",
            window_candles: Number(
                $("window_candles")?.value || 12
            ),
            leaders: Number($("leaders")?.value || 2),
            followers_per_leader: Number(
                $("followers_per_leader")?.value || 3
            ),
            min_similarity: Number(
                $("min_similarity")?.value || 50
            ),
            update_seconds: Number(
                $("update_seconds")?.value || 240
            ),
            closed_candles_only:
                $("closed_candles_only")?.checked ?? true,
            automatic_pairs:
                $("automatic_pairs")?.checked ?? true
        };
    }

    function fillConfigForm(config = {}) {
        const fields = {
            timeframe: config.timeframe ?? "5min",
            window_candles: config.window_candles ?? 12,
            leaders: config.leaders ?? 2,
            followers_per_leader: config.followers_per_leader ?? 3,
            min_similarity: config.min_similarity ?? 50,
            update_seconds: config.update_seconds ?? 240
        };

        for (const [id, value] of Object.entries(fields)) {
            if ($(id))$(id).value = value;
        }

        if ($("closed_candles_only")) {
            $("closed_candles_only").checked = config.closed_candles_only ?? true;
        }

        if ($("automatic_pairs")) {
            $("automatic_pairs").checked = config.automatic_pairs ?? true;
        }
    }

    function updateKeyStatuses(config = {}) {
        const keys = config.api_keys || [];
        let configuredCount = 0;

        for (let number = 1; number <= 4; number++) {
            let isConfigured = false;

            if (Array.isArray(keys)) {
                const entry = keys.find((item) => Number(item?.number) === number);
                isConfigured = Boolean(entry?.configured);
            } else if (keys && typeof keys === "object") {
                isConfigured = Boolean(keys[`api_key_${number}`] || keys[number]);
            }

            const input = $(`api-key-${number}`);
            const statusSpan = $(`key-status-${number}`);

            if (isConfigured) {
                configuredCount++;
                if (input && (!input.value || input.value === "")) {
                    input.value = "••••••••••••••••";
                }
                if (statusSpan && statusSpan.textContent !== "Conectada ✓" && statusSpan.textContent !== "Falhou ✗") {
                    statusSpan.textContent = "Configurada ✓";
                    statusSpan.className = "text-[9px] text-emerald-400 font-semibold";
                }
            }
        }

        updateKeysCard(configuredCount);
    }

    async function loadConfig() {
        try {
            const response = await api("/api/config");

            const config =
                response.config ||
                response.settings ||
                response;

            fillConfigForm(config);
            updateMetrics(config, response);
            updateKeyStatuses(config);

            return config;
        } catch (error) {
            showMessage(
                "config-message",
                `Erro ao carregar configurações: ${error.message}`,
                "error"
            );

            return null;
        }
    }

    async function saveConfig(event) {
        if (event) event.preventDefault();

        const button = $("save-config");

        setLoading(button, true, "Salvando...");

        try {
            const response = await api("/api/config", {
                method: "POST",
                body: JSON.stringify(getConfigFromForm())
            });

            showMessage(
                "config-message",
                response.message || "Configurações salvas com sucesso!",
                "success"
            );

            await loadConfig();
            await refreshState();
        } catch (error) {
            showMessage(
                "config-message",
                `Falha ao salvar: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
        }
    }

    // =====================================================
    // MÉTRICAS
    // =====================================================

    function getConfiguredKeysCount(config = {}, state = {}) {
        if (Number.isFinite(Number(config.api_keys_count))) {
            return Number(config.api_keys_count);
        }

        if (Number.isFinite(Number(state.api_keys_count))) {
            return Number(state.api_keys_count);
        }

        const keys = config.api_keys || config.keys || [];

        if (Array.isArray(keys)) {
            return keys.filter((item) => {
                if (typeof item === "boolean") return item;

                return Boolean(
                    item &&
                    (
                        item.configured === true ||
                        item.has_key === true ||
                        item.saved === true ||
                        item.status === "configured" ||
                        item.status === "ok"
                    )
                );
            }).length;
        }

        return 0;
    }

    function updateKeysCard(count) {
        const cards = document.querySelectorAll("main > div > div");

        for (const card of cards) {
            const heading = card.querySelector("span");

            if (
                heading &&
                heading.textContent.trim() === "Chaves Ativas"
            ) {
                const value = card.querySelector("p.text-sm");

                if (value) {
                    value.textContent = `${count} / 4`;
                }

                break;
            }
        }
    }

    function updateMetrics(config = {}, state = {}) {
        setText("metric-leaders", config.leaders ?? "—");

        setText(
            "metric-followers",
            config.followers_per_leader ?? "—"
        );

        setText(
            "metric-similarity",
            config.min_similarity !== undefined
                ? `${config.min_similarity}%`
                : "—"
        );

        const count = getConfiguredKeysCount(config, state);

        setText("metric-keys", `${count}/4`);
        updateKeysCard(count);
    }

    // =====================================================
    // CHAVES DE API
    // =====================================================

    async function saveKey(number) {
        const input = $(`api-key-${number}`);
        const button = document.querySelector(
            `[data-save-key="${number}"]`
        );

        const key = input?.value.trim() || "";

        if (!key || key === "••••••••••••••••") {
            showMessage(
                "key-results",
                `Digite uma nova chave da API ${number} antes de salvar.`,
                "warning"
            );
            return;
        }

        setLoading(button, true, "Salvando...");

        try {
            const response = await api(`/api/keys/${number}`, {
                method: "POST",
                body: JSON.stringify({ api_key: key })
            });

            showMessage(
                "key-results",
                response.message || `Chave ${number} salva com sucesso!`,
                "success"
            );

            await loadConfig();
            await refreshState();
        } catch (error) {
            showMessage(
                "key-results",
                `Erro ao salvar a chave ${number}: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
        }
    }

    async function testKey(number) {
        const input = $(`api-key-${number}`);
        const button = document.querySelector(`[data-test-key="${number}"]`);
        const statusSpan = $(`key-status-${number}`);

        const key = input?.value.trim() || "";

        if (statusSpan) {
            statusSpan.textContent = "A testar...";
            statusSpan.className = "text-[9px] text-sky-400 font-semibold";
        }

        setLoading(button, true, "Testando...");

        try {
            const response = await api(`/api/keys/test/${number}`, {
                method: "POST",
                body: JSON.stringify({ api_key: key })
            });

            if (response.success && response.test_success) {
                if (statusSpan) {
                    statusSpan.textContent = "Conectada ✓";
                    statusSpan.className = "text-[9px] text-emerald-400 font-semibold";
                }
                showMessage(
                    "key-results",
                    `API ${number}: Conexão com a Twelve Data confirmada com sucesso.`,
                    "success"
                );
            } else {
                if (statusSpan) {
                    statusSpan.textContent = "Falhou ✗";
                    statusSpan.className = "text-[9px] text-rose-400 font-semibold";
                }
                showMessage(
                    "key-results",
                    `API ${number}: ${response.message || "Falha ao validar chave."}`,
                    "error"
                );
            }

            await loadConfig();
            await refreshState();
        } catch (error) {
            if (statusSpan) {
                statusSpan.textContent = "Falhou ✗";
                statusSpan.className = "text-[9px] text-rose-400 font-semibold";
            }
            showMessage(
                "key-results",
                `Erro ao testar a chave ${number}: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
        }
    }

    async function testAllKeys() {
        const button = $("test-all-keys");

        setLoading(button, true, "Testando...");

        try {
            const response = await api("/api/keys/test-all", {
                method: "POST",
                body: JSON.stringify({})
            });

            const results = response.results;

            let message = response.message || "Teste concluído.";

            if (Array.isArray(results) && results.length > 0) {
                message += "\n" + results.map((item) => {
                    if (typeof item === "string") return item;

                    const number =
                        item.number ??
                        item.key_number ??
                        item.id ??
                        "";

                    const status =
                        item.message ??
                        item.status ??
                        item.error ??
                        "Resultado recebido";

                    return `API ${number}: ${status}`;
                }).join("\n");
            }

            showMessage(
                "key-results",
                message,
                response.success === false ? "warning" : "success"
            );

            await loadConfig();
            await refreshState();
        } catch (error) {
            showMessage(
                "key-results",
                `Erro ao testar as chaves: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
        }
    }

    // =====================================================
    // CONTROLE DA ANÁLISE
    // =====================================================

    function setAnalysisStatus(enabled) {
        analysisEnabled = Boolean(enabled);

        setText(
            "statusText",
            analysisEnabled ? "Ligada" : "Desligada"
        );

        setText(
            "system-analysis-status",
            analysisEnabled
                ? "Análise ativada"
                : "Análise desativada"
        );

        const dot = $("statusDot");

        if (dot) {
            dot.classList.remove(
                "bg-slate-500",
                "bg-emerald-400",
                "bg-rose-500"
            );

            dot.classList.add(
                analysisEnabled ? "bg-emerald-400" : "bg-slate-500"
            );
        }

        const startButton = $("start-analysis");
        const stopButton = $("stop-analysis");

        if (startButton) {
            startButton.disabled = analysisEnabled;
            startButton.classList.toggle("opacity-50", analysisEnabled);
            startButton.classList.toggle("cursor-not-allowed", analysisEnabled);
        }

        if (stopButton) {
            stopButton.disabled = !analysisEnabled;
            stopButton.classList.toggle("opacity-50", !analysisEnabled);
            stopButton.classList.toggle("cursor-not-allowed", !analysisEnabled);
        }
    }

    async function changeAnalysisState(desiredEnabled) {
        if (requestInProgress) return;

        if (analysisEnabled === Boolean(desiredEnabled)) {
            showMessage(
                "analysis-message",
                desiredEnabled
                    ? "A análise já está ativada."
                    : "A análise já está desativada.",
                "info"
            );
            return;
        }

        const button = desiredEnabled
            ? $("start-analysis")
            : $("stop-analysis");

        setLoading(
            button,
            true,
            desiredEnabled ? "Iniciando..." : "Parando..."
        );

        try {
            const response = await api("/api/toggle", {
                method: "POST",
                body: JSON.stringify({
                    enabled: Boolean(desiredEnabled)
                })
            });

            showMessage(
                "analysis-message",
                response.message ||
                    "Solicitação enviada. Atualizando estado...",
                "success"
            );

            await refreshState();
        } catch (error) {
            showMessage(
                "analysis-message",
                `Não foi possível alterar a análise: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
            await refreshState();
        }
    }

    // =====================================================
    // RESULTADOS
    // =====================================================

    function parseResults(value) {
        if (!value) return {};

        if (typeof value === "string") {
            try {
                return JSON.parse(value);
            } catch {
                return {};
            }
        }

        return value;
    }

    function getLeaders(results) {
        results = parseResults(results);

        if (Array.isArray(results)) return results;
        if (Array.isArray(results.leaders)) return results.leaders;
        if (Array.isArray(results.results)) return results.results;
        if (Array.isArray(results.data)) return results.data;
        if (Array.isArray(results.items)) return results.items;

        return [];
    }

    function getFirst(object, names, fallback = null) {
        for (const name of names) {
            if (
                object &&
                object[name] !== undefined &&
                object[name] !== null &&
                object[name] !== ""
            ) {
                return object[name];
            }
        }

        return fallback;
    }

    function createElement(tag, className, text) {
        const element = document.createElement(tag);

        if (className) element.className = className;

        if (text !== null && text !== undefined) {
            element.textContent = String(text);
        }

        return element;
    }

    function renderResults(state) {
        const container = $("results-area");
        if (!container) return;

        const results = parseResults(
            state.last_results_json ??
            state.last_results ??
            state.results ??
            null
        );

        const leaders = getLeaders(results);

        setText(
            "results-count",
            `${leaders.length} resultado${leaders.length === 1 ? "" : "s"}`
        );

        container.replaceChildren();

        if (leaders.length === 0) {
            const message = state.analysis_enabled
                ? "Análise ativada. Aguardando resultados do analisador."
                : "Nenhum resultado disponível. Configure as APIs e inicie a análise.";

            container.appendChild(
                createElement(
                    "div",
                    "col-span-full rounded-lg border border-slate-800 bg-slate-900/70 p-4 text-xs text-slate-400",
                    message
                )
            );

            return;
        }

        leaders.forEach((leader, index) => {
            if (!leader || typeof leader !== "object") {
                leader = { symbol: String(leader) };
            }

            const symbol = getFirst(
                leader,
                ["symbol", "pair", "ticker", "name", "asset"],
                `Líder ${index + 1}`
            );

            const card = createElement(
                "article",
                "rounded-lg border border-slate-800 bg-slate-900/70 p-3 text-xs"
            );

            card.appendChild(
                createElement(
                    "h3",
                    "mb-2 font-bold text-sky-400",
                    `Líder ${String(index + 1).padStart(2, "0")} — ${symbol}`
                )
            );

            const score = getFirst(
                leader,
                ["score", "leader_score", "similarity", "similarity_pct"],
                null
            );

            if (score !== null) {
                const similarity =
                    "similarity" in leader ||
                    "similarity_pct" in leader;

                card.appendChild(
                    createElement(
                        "p",
                        "mb-1 text-slate-300",
                        `${similarity ? "Similaridade" : "Pontuação"}: ${
                            similarity ? formatPercent(score) : formatNumber(score)
                        }`
                    )
                );
            }

            const followers = getFirst(
                leader,
                [
                    "followers",
                    "related_markets",
                    "similar_markets",
                    "children"
                ],
                []
            );

            if (Array.isArray(followers)) {
                card.appendChild(
                    createElement(
                        "p",
                        "mb-1 font-medium text-slate-300",
                        `Seguidores: ${followers.length}`
                    )
                );

                followers.forEach((follower) => {
                    const followerName =
                        typeof follower === "object" && follower !== null
                            ? getFirst(
                                follower,
                                ["symbol", "pair", "ticker", "name", "asset"],
                                "Ativo"
                            )
                            : follower;

                    card.appendChild(
                        createElement(
                            "p",
                            "ml-2 mt-1 text-slate-400",
                            `• ${followerName}`
                        )
                    );
                });
            }

            container.appendChild(card);
        });
    }

    // =====================================================
    // GRÁFICO
    // =====================================================

    function initializeChart() {
        const canvas = $("marketChart");

        if (!canvas || typeof Chart === "undefined") return;

        const context = canvas.getContext("2d");

        marketChart = new Chart(context, {
            type: "line",
            data: {
                labels: [],
                datasets: [{
                    label: "Atividade do mercado",
                    data: [],
                    borderColor: "#0284c7",
                    borderWidth: 2,
                    backgroundColor: "rgba(2, 132, 199, 0.15)",
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: "#64748b", font: { size: 10 } }
                    },
                    y: {
                        grid: { color: "#1e293b" },
                        ticks: { color: "#64748b", font: { size: 10 } }
                    }
                }
            }
        });
    }

    function updateChart(state) {
        if (!marketChart) return;

        const activity = state.activity_series;

        if (!Array.isArray(activity)) return;

        marketChart.data.labels = activity.map(
            (item) => item.label ?? item.time ?? ""
        );

        marketChart.data.datasets[0].data = activity.map(
            (item) => Number(item.value ?? item.volume ?? 0)
        );

        marketChart.update("none");
    }

    // =====================================================
    // ATUALIZAÇÃO DO ESTADO
    // =====================================================

    async function refreshState() {
        if (requestInProgress) return null;

        requestInProgress = true;

        try {
            const state = await api("/api/state");
            const config = state.config || state.settings || {};

            setAnalysisStatus(
                state.analysis_enabled ??
                state.enabled ??
                state.is_running ??
                false
            );

            const lastAnalysis =
                state.last_analysis_at ||
                state.last_update ||
                state.updated_at ||
                null;

            setText("lastUpdate", formatDate(lastAnalysis));

            const error =
                state.last_analysis_error ||
                state.analysis_error ||
                "";

            if (error) {
                showMessage("analysis-error", error, "error");
            } else {
                showMessage("analysis-error", "", "info");
            }

            updateMetrics(config, state);
            updateKeyStatuses(config);
            renderResults(state);
            updateChart(state);

            return state;
        } catch (error) {
            showMessage(
                "analysis-error",
                `Falha ao atualizar o painel: ${error.message}`,
                "error"
            );

            setText(
                "system-analysis-status",
                "Sem comunicação com o servidor"
            );

            return null;
        } finally {
            requestInProgress = false;
        }
    }

    // =====================================================
    // EVENTOS
    // =====================================================

    $("config-form")?.addEventListener("submit", saveConfig);

    $("start-analysis")?.addEventListener("click", () => {
        changeAnalysisState(true);
    });

    $("stop-analysis")?.addEventListener("click", () => {
        changeAnalysisState(false);
    });

    $("test-all-keys")?.addEventListener("click", testAllKeys);

    for (let number = 1; number <= 4; number++) {
        document
            .querySelector(`[data-save-key="${number}"]`)
            ?.addEventListener("click", () => saveKey(number));

        document
            .querySelector(`[data-test-key="${number}"]`)
            ?.addEventListener("click", () => testKey(number));
    }

    $("refresh-state")?.addEventListener("click", async () => {
        const button = $("refresh-state");

        setLoading(button, true, "Atualizando...");

        try {
            await loadConfig();
            await refreshState();
        } finally {
            setLoading(button, false);
        }
    });

    // =====================================================
    // INICIALIZAÇÃO
    // =====================================================

    async function initializeDashboard() {
        initializeChart();

        await loadConfig();
        await refreshState();

        if (refreshTimer) {
            window.clearInterval(refreshTimer);
        }

        refreshTimer = window.setInterval(async () => {
            await refreshState();
        }, 10000);
    }

    initializeDashboard();
});