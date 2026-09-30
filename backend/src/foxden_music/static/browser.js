(() => {
  const frame = document.querySelector('.browser-frame');
  const home = document.querySelector('[data-browser-home]');
  const feedback = document.querySelector('[data-browser-feedback]');
  if (!home || !frame) return;
  home.disabled = true;
  const attach = () => {
    home.disabled = true;
    try {
      const doc = frame.contentDocument;
      if (!doc || doc.querySelector('[data-foxden-home-bridge]')) return;
      doc.addEventListener('foxden:home-ready', () => { home.disabled = false; });
      doc.addEventListener('foxden:home-unavailable', () => {
        home.disabled = true;
        feedback.textContent = 'This viewer does not support the home control. Reload to reconnect.';
      });
      doc.addEventListener('foxden:home-result', (event) => {
        home.disabled = false;
        feedback.textContent = event.detail.ok
          ? 'Opening downloader home…'
          : 'Browser is not connected yet. Wait for it to connect, then try again.';
      });
      const script = doc.createElement('script');
      script.type = 'module';
      script.src = home.dataset.bridgeUrl;
      script.dataset.foxdenHomeBridge = '';
      script.addEventListener('error', () => {
        feedback.textContent = 'Browser controls could not load. Reload this page to reconnect.';
      });
      doc.head.appendChild(script);
    } catch (_) {
      feedback.textContent = 'Open the browser through Fox Den to use Downloader home.';
    }
  };
  frame.addEventListener('load', attach);
  if (frame.contentDocument?.readyState === 'complete' && frame.contentDocument.URL !== 'about:blank') attach();
  home.addEventListener('click', () => {
    home.disabled = true;
    feedback.textContent = 'Returning to downloader home…';
    frame.contentDocument.dispatchEvent(new frame.contentWindow.CustomEvent('foxden:home'));
  });
})();
