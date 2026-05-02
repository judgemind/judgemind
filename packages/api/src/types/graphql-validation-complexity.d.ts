declare module 'graphql-validation-complexity' {
  import type { ValidationRule } from 'graphql';

  export interface ComplexityLimitRuleOptions {
    onCost?: (cost: number) => void;
    createError?: (cost: number, node: unknown) => Error;
    formatErrorMessage?: (cost: number) => string;
    scalarCost?: number;
    objectCost?: number;
    listFactor?: number;
    introspectionListFactor?: number;
  }

  export function createComplexityLimitRule(
    maxCost: number,
    options?: ComplexityLimitRuleOptions,
  ): ValidationRule;

  export function complexityLimitExceededErrorMessage(): string;
}
