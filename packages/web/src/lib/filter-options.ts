import { gql, useQuery } from '@apollo/client';

/**
 * Query for distinct county names (used for autocomplete).
 * The dataset is small (~7 counties) so we fetch the full list.
 */
export const DISTINCT_COUNTIES_QUERY = gql`
  query DistinctCounties {
    distinctCounties
  }
`;

/**
 * Query for distinct judge names (used for autocomplete).
 * The dataset is small (~192 judges) so we fetch the full list.
 */
export const DISTINCT_JUDGE_NAMES_QUERY = gql`
  query DistinctJudgeNames {
    distinctJudgeNames
  }
`;

interface CountiesData {
  distinctCounties: string[];
}

interface JudgeNamesData {
  distinctJudgeNames: string[];
}

/** Fetch the list of distinct county names for autocomplete. */
export function useCountyOptions(): string[] {
  const { data } = useQuery<CountiesData>(DISTINCT_COUNTIES_QUERY);
  return data?.distinctCounties ?? [];
}

/** Fetch the list of distinct judge names for autocomplete. */
export function useJudgeNameOptions(): string[] {
  const { data } = useQuery<JudgeNamesData>(DISTINCT_JUDGE_NAMES_QUERY);
  return data?.distinctJudgeNames ?? [];
}
