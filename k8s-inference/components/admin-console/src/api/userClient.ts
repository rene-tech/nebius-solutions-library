import { envelopeRequest } from "./client";
import type {
  AdminApiKeyCreateInput,
  AdminApiKeyDisclosure,
} from "./accessTypes";
import type {
  InferenceUser,
  UserCreate,
  UserDetail,
  UserList,
  UserPatch,
} from "./userTypes";

export const userApi = {
  list: (query: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<UserList>("/users", { query, signal }),
  detail: (id: string, query: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<UserDetail>(`/users/${encodeURIComponent(id)}`, {
      query,
      signal,
    }),
  create: (body: UserCreate, query: URLSearchParams) =>
    envelopeRequest<InferenceUser>("/users", { method: "POST", body, query }),
  update: (id: string, body: UserPatch, query: URLSearchParams) =>
    envelopeRequest<InferenceUser>(`/users/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
      query,
    }),
  createKey: (
    id: string,
    body: AdminApiKeyCreateInput,
    query: URLSearchParams,
  ) =>
    envelopeRequest<AdminApiKeyDisclosure>(
      `/users/${encodeURIComponent(id)}/keys`,
      { method: "POST", body, query },
    ),
};
