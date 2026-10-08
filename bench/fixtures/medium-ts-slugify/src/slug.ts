// URLs amigáveis para os produtos da loja do mini-ERP.

/**
 * Gera o slug de um nome de produto: minúsculas, sem acentos, só [a-z0-9] separados por um único hífen,
 * sem hífen nas pontas, no máximo `max` caracteres (cortando num limite de palavra quando possível).
 */
export function slugify(nome: string, max = 60): string {
  throw new Error("TODO");
}

/** Garante unicidade: se o slug já existe, acrescenta -2, -3, ... */
export function slugUnico(nome: string, existentes: Set<string>, max = 60): string {
  throw new Error("TODO");
}
