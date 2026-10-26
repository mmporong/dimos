import { parseDimosInstall } from "../../gateway/config/environment.ts";
import { runGateway } from "../../gateway/main.ts";
import type { Command } from "./base.ts";

export const runGatewayCommand: Command = async ({ paths, env, bundledSkillsDir }) => {
  await runGateway({
    paths,
    install: parseDimosInstall(env),
    bundledSkillsDir,
    env,
    providers: [],
  });
  return 0;
};
