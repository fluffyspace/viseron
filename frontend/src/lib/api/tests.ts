import {
  UseMutationResult,
  UseQueryResult,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { API_V1_URL, viseronAPI } from "lib/api/client";
import * as types from "lib/types";

const RUNS_KEY = ["tests", "runs"] as const;
const LATEST_KEY = ["tests", "runs", "latest"] as const;
const CASES_KEY = ["tests", "cases"] as const;

/** Absolute URL that streams the clip bytes for a given test result. */
export function testResultClipUrl(resultId: number): string {
  return `${API_V1_URL}/tests/results/${resultId}/clip`;
}

/** Absolute URL that streams the clip bytes for a catalogued test case. */
export function testCaseClipUrl(caseId: number): string {
  return `${API_V1_URL}/tests/cases/${caseId}/clip`;
}

async function fetchRuns(limit = 20): Promise<types.TestRunsListResponse> {
  const response = await viseronAPI.get<types.TestRunsListResponse>(
    "tests/runs",
    { params: { limit } },
  );
  return response.data;
}

async function fetchRun(
  runId: number,
): Promise<types.TestRunDetailResponse> {
  const response = await viseronAPI.get<types.TestRunDetailResponse>(
    `tests/runs/${runId}`,
  );
  return response.data;
}

async function fetchLatestRun(): Promise<types.TestRunDetailResponse> {
  const response = await viseronAPI.get<types.TestRunDetailResponse>(
    "tests/runs/latest",
  );
  return response.data;
}

async function postRun(): Promise<types.TestRunStartResponse> {
  const response = await viseronAPI.post<types.TestRunStartResponse>(
    "tests/runs",
  );
  return response.data;
}

async function postClip(
  payload: types.TestClipCreateRequest,
): Promise<types.TestClipCreateResponse> {
  const response = await viseronAPI.post<types.TestClipCreateResponse>(
    "tests/clips",
    payload,
  );
  return response.data;
}

async function fetchCases(): Promise<types.TestCasesListResponse> {
  const response = await viseronAPI.get<types.TestCasesListResponse>(
    "tests/cases",
  );
  return response.data;
}

async function deleteCase(caseId: number): Promise<{ deleted: number }> {
  const response = await viseronAPI.delete<{ deleted: number }>(
    `tests/cases/${caseId}`,
  );
  return response.data;
}

async function postRestart(): Promise<{ restarting: boolean }> {
  const response = await viseronAPI.post<{ restarting: boolean }>(
    "tests/restart",
  );
  return response.data;
}

export function useTestRuns(
  limit = 20,
): UseQueryResult<types.TestRunsListResponse, types.APIErrorResponse> {
  return useQuery({
    queryKey: [...RUNS_KEY, limit],
    queryFn: () => fetchRuns(limit),
    // Auto-refresh while a run could be in flight.
    refetchInterval: 5000,
  });
}

export function useTestRunDetail(
  runId: number | null,
): UseQueryResult<types.TestRunDetailResponse, types.APIErrorResponse> {
  return useQuery({
    queryKey: [...RUNS_KEY, runId],
    queryFn: () => fetchRun(runId as number),
    enabled: runId !== null,
  });
}

export function useLatestTestRun(): UseQueryResult<
  types.TestRunDetailResponse,
  types.APIErrorResponse
> {
  return useQuery({
    queryKey: LATEST_KEY,
    queryFn: fetchLatestRun,
    refetchInterval: 5000,
  });
}

export function useTriggerTestRun(): UseMutationResult<
  types.TestRunStartResponse,
  types.APIErrorResponse,
  void
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: postRun,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: RUNS_KEY });
      await queryClient.invalidateQueries({ queryKey: LATEST_KEY });
    },
  });
}

export function useCreateTestClip(): UseMutationResult<
  types.TestClipCreateResponse,
  types.APIErrorResponse,
  types.TestClipCreateRequest
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: postClip,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: CASES_KEY });
    },
  });
}

export function useTestCases(): UseQueryResult<
  types.TestCasesListResponse,
  types.APIErrorResponse
> {
  return useQuery({
    queryKey: CASES_KEY,
    queryFn: fetchCases,
  });
}

export function useDeleteTestCase(): UseMutationResult<
  { deleted: number },
  types.APIErrorResponse,
  number
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteCase,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: CASES_KEY });
    },
  });
}

export function useRestartViseron(): UseMutationResult<
  { restarting: boolean },
  types.APIErrorResponse,
  void
> {
  return useMutation({
    mutationFn: postRestart,
  });
}
