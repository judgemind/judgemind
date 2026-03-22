'use strict';

module.exports = {
  rules: {
    'no-client-utility-import': require('../eslint-rules/no-client-utility-import'),
    'no-ssr-incompatible-import': require('../eslint-rules/no-ssr-incompatible-import'),
    'prefer-path-alias': require('../eslint-rules/prefer-path-alias'),
  },
};
