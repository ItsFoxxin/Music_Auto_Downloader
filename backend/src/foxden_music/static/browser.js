(() => {
  const frame = document.querySelector('.browser-frame');
  const refresh = document.querySelector('[data-refresh-browser]');
  if (refresh && frame) {
    refresh.addEventListener('click', () => {
      // Reload the viewer connection, not the remote Firefox process or download.
      frame.src = frame.getAttribute('src');
    });
  } else if (refresh) {
    refresh.disabled = true;
  }
})();
