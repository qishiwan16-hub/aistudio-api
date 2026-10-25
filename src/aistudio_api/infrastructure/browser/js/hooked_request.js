(args) => {
    return new Promise((resolve) => {
        const timeoutMs = (args.timeout || 600) * 1000;
        const controller = new AbortController();
        const timer = timeoutMs > 0 ? setTimeout(() => {
            controller.abort();
            resolve({status: 0, body: 'timeout'});
        }, timeoutMs) : null;

        const headers = Object.assign({}, args.headers || {});

        window.fetch(args.url, {
            method: 'POST',
            headers: headers,
            body: args.gzip ? Uint8Array.from(atob(args.body), c => c.charCodeAt(0)) : args.body,
            credentials: 'include',
            signal: controller.signal,
        }).then(async (response) => {
            try {
                const text = await response.text();
                clearTimeout(timer);
                resolve({status: response.status, body: text});
            } catch (e) {
                clearTimeout(timer);
                const isAbort = e && e.name === 'AbortError';
                const errMsg = isAbort ? 'timeout' : ((e && e.message) ? `response body read error: ${e.message}` : 'response body read error');
                resolve({status: isAbort ? 0 : (response.status || 0), body: isAbort ? 'timeout' : errMsg});
            }
        }).catch((err) => {
            clearTimeout(timer);
            const msg = (err && err.name === 'AbortError') ? 'timeout' : ((err && err.message) || 'network error');
            resolve({status: 0, body: msg});
        });
    });
}
