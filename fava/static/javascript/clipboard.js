const Clipboard = require('clipboard');

module.exports.initClipboard = function initClipboard() {
  new Clipboard('.status-indicator'); // eslint-disable-line no-new
  new Clipboard('#copy-balances', { // eslint-disable-line no-new
    text(trigger) {
      const text = trigger.getAttribute('data-clipboard-text').split('\n');
      text.splice(0, 2);
      return text.join('\n').trim();
    },
  });
};
