/* Shared chart JavaScript — extracted from inline templates.
   Loaded by pages with live LightweightCharts (backtester + backtest detail).

   Required globals (set by inline <script> blocks with Jinja data):
   - __lwData: { price: [], ind1: [], ind2: [], ind1Label: '', ind2Label: '' }
   - __lwAsset: string (asset name for live price polling)
   - __lwVsAsset: string (optional, for ratio charts)
   - lwChartLoaded: boolean (set before this script loads)
*/

var lwChartLoaded = false;
var _livePriceInterval = null;
var _liveChartActive = false;

function startLivePolling() {
    if (_livePriceInterval) return;
    if (typeof __lwAsset === 'undefined') return;
    _liveChartActive = true;
    fetchLivePrice();
    _livePriceInterval = setInterval(fetchLivePrice, 60000);
}
function stopLivePolling() {
    _liveChartActive = false;
    if (_livePriceInterval) { clearInterval(_livePriceInterval); _livePriceInterval = null; }
}
function fetchLivePrice() {
    if (document.hidden) return;
    if (!window._lwPriceSeries) return;
    if (typeof __lwAsset === 'undefined') return;
    var vsAsset = (typeof __lwVsAsset !== 'undefined') ? __lwVsAsset : '';
    if (vsAsset) {
        Promise.all([
            fetch('/api/price-now/' + encodeURIComponent(__lwAsset)).then(function(r) { return r.json(); }),
            fetch('/api/price-now/' + encodeURIComponent(vsAsset)).then(function(r) { return r.json(); })
        ]).then(function(results) {
            var d1 = results[0], d2 = results[1];
            if (d1.error === 'quota' || d2.error === 'quota') { stopLivePolling(); return; }
            if (d1.price && d2.price && d2.price > 0 && d1.time) {
                window._lwPriceSeries.update({time: d1.time, value: d1.price / d2.price});
            }
        }).catch(function(){});
    } else {
        fetch('/api/price-now/' + encodeURIComponent(__lwAsset))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.price && data.time) {
                    window._lwPriceSeries.update({time: data.time, value: data.price});
                }
                if (data.error === 'quota') stopLivePolling();
            })
            .catch(function(){});
    }
}
document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
        if (_livePriceInterval) { clearInterval(_livePriceInterval); _livePriceInterval = null; }
    } else if (_liveChartActive) {
        fetchLivePrice();
        _livePriceInterval = setInterval(fetchLivePrice, 60000);
    }
});

function switchChartTab(tab, btn) {
    var bt = document.getElementById('backtest-chart-tab');
    var lw = document.getElementById('livechart-tab');
    if (!bt || !lw) return;
    bt.style.display = tab === 'backtest' ? '' : 'none';
    lw.style.display = tab === 'livechart' ? '' : 'none';
    var tabs = btn.parentElement.querySelectorAll('.chart-tab');
    for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
    btn.classList.add('active');
    if (tab === 'livechart' && !lwChartLoaded) {
        loadLWChart();
    }
    if (tab === 'livechart') {
        startLivePolling();
    } else {
        stopLivePolling();
    }
    // Update URL with view parameter
    var url = new URL(window.location);
    if (tab === 'livechart') {
        url.searchParams.set('view', 'livechart');
    } else {
        url.searchParams.delete('view');
    }
    history.replaceState(null, '', url.toString());
}
function activateViewFromURL() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('view') === 'livechart') {
        var tabs = document.querySelectorAll('.chart-tab');
        if (tabs.length >= 2) switchChartTab('livechart', tabs[1]);
    }
}
function downloadChart() {
    // Try ID-based lookup first (main backtester), then class-based (detail page)
    var img = document.getElementById('backtest-chart-img') || document.querySelector('.chart-img');
    if (!img) return;
    var asset = document.getElementById('asset');
    var assetName = asset ? asset.value : (typeof __lwAsset !== 'undefined' ? __lwAsset : 'chart');
    var a = document.createElement('a');
    a.href = img.src;
    a.download = assetName + '_backtest.png';
    a.click();
}
function loadLWChart() {
    var container = document.getElementById('lw-chart-container');
    if (!container || typeof __lwData === 'undefined') return;
    lwChartLoaded = true;
    container.innerHTML = '';

    var priceData = __lwData.price || [];
    var ind1Data = __lwData.ind1 || [];
    var ind2Data = __lwData.ind2 || [];
    var ind1Label = __lwData.ind1Label || '';
    var ind2Label = __lwData.ind2Label || '';

    if (priceData.length === 0) return;

    // Determine decimal precision from data magnitude
    function calcPriceFormat(data) {
        if (!data || data.length === 0) return { precision: 2, minMove: 0.01 };
        var vals = data.map(function(d) { return Math.abs(d.value); }).filter(function(v) { return v > 0; });
        if (vals.length === 0) return { precision: 2, minMove: 0.01 };
        var median = vals.sort(function(a,b){return a-b;})[Math.floor(vals.length/2)];
        var prec;
        if (median >= 1) prec = 2;
        else if (median >= 0.1) prec = 3;
        else if (median >= 0.01) prec = 4;
        else if (median >= 0.001) prec = 5;
        else if (median >= 0.0001) prec = 6;
        else prec = 8;
        return { precision: prec, minMove: Math.pow(10, -prec) };
    }
    var priceFmt = calcPriceFormat(priceData);

    var chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 600,
        layout: {
            background: { color: '#161922' },
            textColor: '#8890a4',
            fontFamily: "'DM Sans', sans-serif"
        },
        grid: {
            vertLines: { color: '#252a3a' },
            horzLines: { color: '#252a3a' }
        },
        rightPriceScale: {
            mode: LightweightCharts.PriceScaleMode.Logarithmic,
            borderColor: '#252a3a'
        },
        timeScale: {
            borderColor: '#252a3a',
            timeVisible: false
        },
        crosshair: {
            horzLine: { color: '#555d74', labelBackgroundColor: '#252a3a' },
            vertLine: { color: '#555d74', labelBackgroundColor: '#252a3a' }
        }
    });

    var priceSeries = chart.addSeries(LightweightCharts.LineSeries, {
        color: '#e8eaf0',
        lineWidth: 2,
        title: 'Price',
        priceLineVisible: false,
        priceFormat: { type: 'price', precision: priceFmt.precision, minMove: priceFmt.minMove }
    });
    priceSeries.setData(priceData);
    window._lwPriceSeries = priceSeries;

    if (ind2Data.length > 0) {
        var ind2Fmt = calcPriceFormat(ind2Data);
        var ind2Series = chart.addSeries(LightweightCharts.LineSeries, {
            color: '#6495ED',
            lineWidth: 2,
            title: ind2Label,
            priceLineVisible: false,
            priceFormat: { type: 'price', precision: ind2Fmt.precision, minMove: ind2Fmt.minMove }
        });
        ind2Series.setData(ind2Data);
    }

    if (ind1Data.length > 0) {
        var ind1Fmt = calcPriceFormat(ind1Data);
        var ind1Series = chart.addSeries(LightweightCharts.LineSeries, {
            color: '#f7931a',
            lineWidth: 2,
            title: ind1Label,
            priceLineVisible: false,
            priceFormat: { type: 'price', precision: ind1Fmt.precision, minMove: ind1Fmt.minMove }
        });
        ind1Series.setData(ind1Data);
    }

    // Default zoom: show last 12 months
    if (priceData.length > 0) {
        var lastPoint = priceData[priceData.length - 1];
        var lastDate = new Date(lastPoint.time);
        var fromDate = new Date(lastDate);
        fromDate.setFullYear(fromDate.getFullYear() - 1);
        chart.timeScale().setVisibleRange({
            from: fromDate.toISOString().split('T')[0],
            to: lastPoint.time
        });
    } else {
        chart.timeScale().fitContent();
    }

    // Ticker watermark
    var wm = document.createElement('div');
    wm.textContent = (typeof __lwAsset !== 'undefined' ? __lwAsset : '').toUpperCase();
    wm.style.cssText = 'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:"JetBrains Mono",monospace;font-size:80px;font-weight:700;color:rgba(255,255,255,0.04);pointer-events:none;z-index:1;user-select:none;letter-spacing:4px;white-space:nowrap;';
    container.appendChild(wm);

    window.addEventListener('resize', function() {
        chart.applyOptions({ width: container.clientWidth });
    });

    var activeTool = null;  // 'measure' | 'draw' | null
    var isLogScale = true;

    function setTool(tool) {
        if (activeTool === tool) { activeTool = null; } else { activeTool = tool; }
        container.style.cursor = activeTool ? 'crosshair' : '';
        // Cancel any in-progress action
        if (activeTool !== 'measure') removeMeasure();
        if (activeTool !== 'draw') cancelDraw();
    }

    function toggleScale() {
        isLogScale = !isLogScale;
        chart.applyOptions({
            rightPriceScale: {
                mode: isLogScale ? LightweightCharts.PriceScaleMode.Logarithmic : LightweightCharts.PriceScaleMode.Normal
            }
        });
    }

    function clearAll() {
        removeMeasure();
        cancelDraw();
        drawnLines.forEach(function(item) { item.remove(); });
        drawnLines = [];
    }

    // ===== Shared SVG helpers =====
    function makeSvgOverlay() {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:4;';
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        return svg;
    }
    function addDot(svg, cx, cy, color) {
        var c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        c.setAttribute('cx', cx); c.setAttribute('cy', cy); c.setAttribute('r', '4');
        c.setAttribute('fill', color);
        svg.appendChild(c);
    }
    function addLine(svg, x1, y1, x2, y2, color, dash) {
        var l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        l.setAttribute('x1', x1); l.setAttribute('y1', y1);
        l.setAttribute('x2', x2); l.setAttribute('y2', y2);
        l.setAttribute('stroke', color); l.setAttribute('stroke-width', '1.5');
        if (dash) l.setAttribute('stroke-dasharray', dash);
        l.setAttribute('opacity', '0.8');
        svg.appendChild(l);
    }
    function createLabel(text, x, y) {
        var el = document.createElement('div');
        el.className = 'lw-measure-label';
        el.innerHTML = text;
        el.style.left = x + 'px';
        el.style.top = y + 'px';
        container.appendChild(el);
        var rect = el.getBoundingClientRect();
        var cRect = container.getBoundingClientRect();
        if (rect.right > cRect.right - 4) el.style.left = (x - rect.width - 8) + 'px';
        if (rect.bottom > cRect.bottom - 4) el.style.top = (y - rect.height - 8) + 'px';
        return el;
    }
    function formatMeasure(startPrice, endPrice) {
        if (!startPrice || startPrice <= 0) return '';
        var pctChange = ((endPrice - startPrice) / startPrice * 100);
        var sign = pctChange >= 0 ? '+' : '';
        var color = pctChange >= 0 ? '#34d399' : '#ef4444';
        return '<span style="color:' + color + ';font-weight:600;font-size:13px">' + sign + pctChange.toFixed(2) + '%</span>' +
               '<br><span style="color:#8890a4;font-size:11px">' + (endPrice - startPrice >= 0 ? '+' : '') + (endPrice - startPrice).toFixed(priceFmt.precision) + '</span>';
    }

    // ===== Measure tool =====
    var measureStart = null, measureLabel = null, measureLine = null, measureActive = false;
    function removeMeasure() {
        if (measureLabel) { measureLabel.remove(); measureLabel = null; }
        if (measureLine) { measureLine.remove(); measureLine = null; }
        measureStart = null; measureActive = false;
    }

    // ===== Draw lines tool =====
    var drawStart = null, drawPreview = null, drawActive = false;
    var drawnLines = [];  // persistent lines [{svg, ...}]
    function cancelDraw() {
        if (drawPreview) { drawPreview.remove(); drawPreview = null; }
        drawStart = null; drawActive = false;
    }

    // ===== Click handler =====
    container.addEventListener('click', function(e) {
        var cRect = container.getBoundingClientRect();
        var x = e.clientX - cRect.left;
        var y = e.clientY - cRect.top;

        // Shift+Click always activates measure regardless of active tool
        if (e.shiftKey) {
            if (activeTool !== 'measure') setTool('measure');
        }

        if (activeTool === 'measure') {
            var price = priceSeries.coordinateToPrice(y);
            if (!measureStart) {
                removeMeasure();
                measureStart = { x: x, y: y, price: price };
                measureActive = true;
                var svg = makeSvgOverlay(); addDot(svg, x, y, '#f7931a'); addLine(svg, x, y, x, y, '#f7931a', '6,3');
                container.appendChild(svg); measureLine = svg;
            } else {
                if (measureLine) measureLine.remove();
                var svg = makeSvgOverlay();
                addLine(svg, measureStart.x, measureStart.y, x, y, '#f7931a', '6,3');
                addDot(svg, measureStart.x, measureStart.y, '#f7931a');
                addDot(svg, x, y, '#f7931a');
                container.appendChild(svg); measureLine = svg;
                if (measureStart.price && price && measureStart.price > 0 && price > 0) {
                    measureLabel = createLabel(formatMeasure(measureStart.price, price),
                        Math.max(measureStart.x, x) + 12, Math.min(measureStart.y, y) - 8);
                }
                measureStart = null; measureActive = false;
            }
            return;
        }

        if (activeTool === 'draw') {
            if (!drawStart) {
                drawStart = { x: x, y: y };
                drawActive = true;
                var svg = makeSvgOverlay(); addDot(svg, x, y, '#6495ED'); addLine(svg, x, y, x, y, '#6495ED');
                container.appendChild(svg); drawPreview = svg;
            } else {
                if (drawPreview) drawPreview.remove(); drawPreview = null;
                var svg = makeSvgOverlay();
                addLine(svg, drawStart.x, drawStart.y, x, y, '#6495ED');
                addDot(svg, drawStart.x, drawStart.y, '#6495ED');
                addDot(svg, x, y, '#6495ED');
                container.appendChild(svg);
                drawnLines.push(svg);
                drawStart = null; drawActive = false;
            }
            return;
        }

        // No tool active — plain click clears measure if any
        if (measureStart) removeMeasure();
    });

    // ===== Mousemove for live preview =====
    container.addEventListener('mousemove', function(e) {
        var cRect = container.getBoundingClientRect();
        var x = e.clientX - cRect.left;
        var y = e.clientY - cRect.top;

        if (measureActive && measureStart) {
            if (measureLine) measureLine.remove();
            if (measureLabel) { measureLabel.remove(); measureLabel = null; }
            var svg = makeSvgOverlay();
            addLine(svg, measureStart.x, measureStart.y, x, y, '#f7931a', '6,3');
            addDot(svg, measureStart.x, measureStart.y, '#f7931a');
            addDot(svg, x, y, '#f7931a');
            container.appendChild(svg); measureLine = svg;
            var price = priceSeries.coordinateToPrice(y);
            if (measureStart.price && price && measureStart.price > 0 && price > 0) {
                measureLabel = createLabel(formatMeasure(measureStart.price, price),
                    Math.max(measureStart.x, x) + 12, Math.min(measureStart.y, y) - 8);
            }
        }

        if (drawActive && drawStart) {
            if (drawPreview) drawPreview.remove();
            var svg = makeSvgOverlay();
            addLine(svg, drawStart.x, drawStart.y, x, y, '#6495ED');
            addDot(svg, drawStart.x, drawStart.y, '#6495ED');
            addDot(svg, x, y, '#6495ED');
            container.appendChild(svg); drawPreview = svg;
        }
    });

    // Keyboard shortcuts for chart tools
    document.addEventListener('keydown', function(e) {
        // Skip if user is typing in an input/textarea/select
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
        var key = e.key.toLowerCase();
        if (key === 'escape') {
            if (measureActive) removeMeasure();
            if (drawActive) cancelDraw();
            if (activeTool) setTool(null);
        } else if (key === 'm') {
            setTool('measure');
        } else if (key === 'd') {
            setTool('draw');
        } else if (key === 'l') {
            toggleScale();
        } else if (key === 'c') {
            clearAll();
        }
    });
}
