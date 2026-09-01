import { redirect } from "next/navigation";

/** Персонализация переехала в «Диалоги»: написание черновиков — на «Холодных»,
 *  диалоги с ответами — здесь. Редирект вместо 404 для старых закладок. */
export default function Page() {
  redirect("/threads");
}
