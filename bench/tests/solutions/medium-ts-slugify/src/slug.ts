// URLs amigáveis para os produtos da loja do mini-ERP.

export function slugify(nome: string, max = 60): string {
  const base = nome
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (base.length <= max) return base;
  const cortado = base.slice(0, max);
  const ultimo = cortado.lastIndexOf("-");
  return (ultimo > 0 && base[max] !== "-" ? cortado.slice(0, ultimo) : cortado).replace(/-+$/g, "");
}

export function slugUnico(nome: string, existentes: Set<string>, max = 60): string {
  const base = slugify(nome, max);
  if (!existentes.has(base)) return base;
  let n = 2;
  while (existentes.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}
