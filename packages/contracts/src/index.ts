export type ContractGenerationStatus = "pending-client-phase";

export interface ContractPackageInfo {
  status: ContractGenerationStatus;
  note: string;
}

export const contractPackageInfo: ContractPackageInfo = {
  status: "pending-client-phase",
  note: "OpenAPI-driven TS generation will be added when client implementation begins.",
};
