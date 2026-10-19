import {
  Color,
  Credit,
  ImageryLayer,
  Ion,
  Terrain,
  TileMapServiceImageryProvider,
  UrlTemplateImageryProvider,
  Viewer,
  buildModuleUrl,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import type { AppConfig } from "../config";

const NATURAL_EARTH_CREDIT = new Credit("Natural Earth II (public domain)", false);

/**
 * CesiumJS viewer that needs no paid token: Natural Earth II (bundled with
 * CesiumJS, so it renders even offline) under a dimmed XYZ tile layer
 * (OpenStreetMap by default) on the WGS84 ellipsoid. With a Cesium ion token,
 * Cesium World Terrain (and World Imagery, unless overridden) are used. The
 * ion geocoder is only enabled with a token; the base layer picker, which
 * lists ion layers, is always off.
 */
export function createViewer(container: HTMLElement, config: AppConfig): Viewer {
  const { ionToken } = config;
  if (ionToken) Ion.defaultAccessToken = ionToken;

  const baseLayer = config.imagery.kind === "ion"
    ? ImageryLayer.fromWorldImagery({ brightness: 0.75 })
    : ImageryLayer.fromProviderAsync(
        TileMapServiceImageryProvider.fromUrl(buildModuleUrl("Assets/Textures/NaturalEarthII"), {
          credit: NATURAL_EARTH_CREDIT,
        }),
      );

  const viewer = new Viewer(container, {
    baseLayer,
    ...(ionToken ? { terrain: Terrain.fromWorldTerrain() } : {}),
    geocoder: ionToken !== null,
    baseLayerPicker: false,
    animation: false,
    timeline: false,
    fullscreenButton: false,
    vrButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    homeButton: true,
    infoBox: false,
    selectionIndicator: false,
    // Render only when something changes (the layers request frames); saves
    // battery on phones left open at night.
    requestRenderMode: true,
    maximumRenderTimeChange: Number.POSITIVE_INFINITY,
  });

  if (config.imagery.kind === "tiles") {
    const tiles = viewer.imageryLayers.addImageryProvider(
      new UrlTemplateImageryProvider({
        url: config.imagery.url,
        maximumLevel: 19,
        credit: new Credit(config.imagery.credit, true),
      }),
    );
    // Night-friendly: darker and less saturated, so overlays stay readable.
    tiles.brightness = 0.62;
    tiles.contrast = 1.15;
    tiles.saturation = 0.55;
  }

  const { scene } = viewer;
  scene.backgroundColor = Color.fromCssColorString("#03050a");
  scene.globe.baseColor = Color.fromCssColorString("#0b1220");
  scene.globe.enableLighting = false;
  return viewer;
}
