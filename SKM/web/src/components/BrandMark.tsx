/** ログイン・favicon で同じ PNG を使い、製品印の形を一元化する。 */
export function BrandMark() {
  return <img className="brandMark" src={`${import.meta.env.BASE_URL}favicon.png`} width={38} height={38} alt="" aria-hidden="true" />
}
