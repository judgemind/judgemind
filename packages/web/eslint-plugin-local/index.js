'use strict';

module.exports = {
  rules: {
    'clickable-table-row': require('../eslint-rules/clickable-table-row'),
    'no-client-utility-import': require('../eslint-rules/no-client-utility-import'),
    'no-ssr-incompatible-import': require('../eslint-rules/no-ssr-incompatible-import'),
    'polled-query-error-guard': require('../eslint-rules/polled-query-error-guard'),
    'prefer-path-alias': require('../eslint-rules/prefer-path-alias'),
  },
};
