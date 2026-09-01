import { redirect } from "next/navigation";

/** Страница разъехалась на /leads (выдача) и /leads/[id] (карточка).
 *  Редирект, а не удаление: ссылки в закладках оператора не должны отдавать 404. */
export default function Page() {
  redirect("/leads");
}
